"""Checks a report's text against itself, with no table and no LLM.

Three mistakes no workbook can show, all planted by the mentor in SKDU Tw III-2026:
  - a sentence that compares two of its own numbers the wrong way round ("22,68%, lebih rendah
    dibandingkan 22,34%") — the numbers are right, the word is not;
  - a sentence citing the wrong appendix table ("akses kredit … (Lampiran Tabel 1)", the
    Kegiatan Usaha table, while its figures are printed in Tabel 3) — told from what each table
    of a Word document prints, read out of its picture, never from the Excel;
  - the appendix's own numbering ("Tabel 10" printed twice, no Tabel 9).

Each finding is one Tidak Sesuai card (operation "comparison" or "reference"), added after the
claims are judged, so it never changes how a claim is read.
"""
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Set, Tuple

from schemas import FactVerificationResult
from structured_extractor import (
    _PAGE_MARKER_PATTERN,
    _SENT_START_RE,
    _filter_narrative,
    _normalize_ws,
    _parse_indonesian_number,
)

RESOLVED_VIA = "document_check"

# ---------------------------------------------------------------------------
# Sentences
# ---------------------------------------------------------------------------


def _sentences(narrative_text: str) -> List[Tuple[str, Optional[int]]]:
    """The narrative's sentences (the same lines the extractor reads), each with its page."""
    out: List[Tuple[str, Optional[int]]] = []
    for block in re.split(r"(?=\[== Halaman \d+ ==\])", _filter_narrative(narrative_text)):
        marker = _PAGE_MARKER_PATTERN.search(block)
        page = int(marker.group(1)) if marker else None
        text = _normalize_ws(_PAGE_MARKER_PATTERN.sub(" ", block)).strip()
        starts = [0] + [m.end() for m in _SENT_START_RE.finditer(text)]
        for a, b in zip(starts, starts[1:] + [len(text)]):
            sentence = text[a:b].strip()
            if sentence:
                out.append((sentence, page))
    return out


# ---------------------------------------------------------------------------
# A comparison between two numbers of the same sentence
# ---------------------------------------------------------------------------

_NUMBER = r"-?\d+(?:\.\d{3})*(?:,\d+)?"
_NUMBER_RE = re.compile(rf"(?<![\w,.]){_NUMBER}(?![\w]|[.,]\d)")
# "lebih tinggi dibandingkan", "lebih rendah dari", "lebih besar daripada". Not "lebih tinggi
# 0,3 poin dari": the connector has to follow the direction word.
_COMPARISON_RE = re.compile(
    r"\blebih\s+(tinggi|rendah|besar|kecil)\s+(?:dibandingkan|dibanding|daripada|dari)\b", re.IGNORECASE)
_HIGHER = {"tinggi", "besar"}
# How far after the connector the compared number may sit: "dibandingkan SB 2,45%",
# "dibandingkan triwulan sebelumnya yang sebesar 12,97%".
_AFTER_WINDOW = 60


class _Unsure(Exception):
    """A figure whose sign the words leave open: the comparison is not judged at all."""


# "terkontraksi sebesar 12,27%" is −12,27: the sentence states the size, the word the sign.
_NEGATIVE_WORD_RE = re.compile(r"\b(?:terkontraksi|kontraksi|minus)\b[^\d]{0,20}$", re.IGNORECASE)
# "turun 1,2%" may be a fall of 1,2 or a growth of −1,2: either reading is the sentence's.
_FALL_WORD_RE = re.compile(r"\b(?:turun|menurun|penurunan)\b(?:\s+sebesar)?\s*$", re.IGNORECASE)


def _plain_numbers(text: str) -> List[Tuple[float, bool]]:
    """(value, has_percent) of each figure in `text`, without the years ("2026"), the table and
    chart numbers ("Tabel 3", "Grafik 2") and a bound ("(indeks >100)"). Raises _Unsure on a
    figure only a fall word sizes."""
    found: List[Tuple[float, bool]] = []
    for m in _NUMBER_RE.finditer(text):
        raw = m.group(0)
        if re.fullmatch(r"(19|20)\d\d", raw):
            continue
        before = text[max(0, m.start() - 30):m.start()]
        if re.search(r"(tabel|grafik|gambar|lampiran|halaman|boks)\s*$", before, re.IGNORECASE):
            continue
        if re.search(r"[<>≤≥=]\s*$", before):
            continue
        value = _parse_indonesian_number(raw)
        if value is None:
            continue
        if _FALL_WORD_RE.search(before):
            raise _Unsure()
        if value > 0 and _NEGATIVE_WORD_RE.search(before):
            value = -value
        found.append((value, text[m.end():m.end() + 2].lstrip().startswith("%")))
    return found


def _fmt(value: float) -> str:
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


_RESPECTIVELY_RE = re.compile(r"\bmasing-masing\b", re.IGNORECASE)
# Where the compared side of the sentence ends: the next clause.
_CLAUSE_END_RE = re.compile(r";|\b(?:sementara|namun|sedangkan)\b", re.IGNORECASE)


def _pairs(sentence: str, m: "re.Match", head: str) -> List[Tuple[Tuple[float, bool], Tuple[float, bool]]]:
    """The figures the comparison at `m` sets against each other. "A1 dan A2 masing-masing …,
    lebih rendah dari B1 dan B2" compares A1 with B1 and A2 with B2; otherwise the last figure
    before the words with the first one after them."""
    respectively = list(_RESPECTIVELY_RE.finditer(head))
    if respectively:
        before = _plain_numbers(head[respectively[-1].end():])
        if len(before) >= 2:
            tail = sentence[m.end():]
            end = _CLAUSE_END_RE.search(tail)
            after = _plain_numbers(tail[:end.start()] if end else tail)
            if len(after) < len(before):
                return []
            return list(zip(before, after[:len(before)]))
    before = _plain_numbers(head)
    after = _plain_numbers(sentence[m.end():m.end() + _AFTER_WINDOW])
    return [(before[-1], after[0])] if before and after else []


def _wrong_comparisons(sentence: str) -> List[Tuple[float, float, str]]:
    """(a, b, word) for each "a … lebih <word> dibandingkan b" the two numbers contradict."""
    wrong: List[Tuple[float, float, str]] = []
    previous_end = 0
    for m in _COMPARISON_RE.finditer(sentence):
        head = sentence[previous_end:m.start()]
        previous_end = m.end()
        if re.search(r"\btidak\s+$", head, re.IGNORECASE):
            continue
        try:
            pairs = _pairs(sentence, m, head)
        except _Unsure:
            continue
        higher = m.group(1).lower() in _HIGHER
        for (a, a_pct), (b, b_pct) in pairs:
            # "kontraksi 0,6%, lebih rendah dibandingkan kontraksi 1,3%" (SULNI) compares the
            # sizes of two contractions: between two negatives the words may mean either way.
            if a < 0 and b < 0:
                continue
            if a_pct == b_pct and a != b and (a > b) != higher:
                wrong.append((a, b, m.group(1).lower()))
    return wrong


def _comparison_result(sentence: str, page: Optional[int], a: float, b: float, word: str
                       ) -> FactVerificationResult:
    truth = "lebih tinggi" if a > b else "lebih rendah"
    return FactVerificationResult(
        operation="comparison", metric_label=f"Perbandingan {_fmt(a)} dan {_fmt(b)}",
        verdict="Refuted", context_quote=sentence, page_number=page, resolved_via=RESOLVED_VIA,
        reasoning=(f"Kalimat menulis {_fmt(a)} lebih {word} dibandingkan {_fmt(b)}, padahal {_fmt(a)} "
                   f"{truth} dari {_fmt(b)} | Dicek dari dua angka di kalimat itu sendiri, tanpa tabel"),
    )


def _close(x: Optional[float], y: float) -> bool:
    return x is not None and abs(x - y) <= 0.005 + 1e-9


def _is_same_comparison(result: FactVerificationResult, sentence: str, a: float, b: float) -> bool:
    """The extracted trend claim of the same sentence over the same two numbers: it read the
    comparison the way the numbers go, not the way the sentence words it."""
    if result.operation not in ("is_increasing", "is_decreasing") or _normalize_ws(result.context_quote) != sentence:
        return False
    values = [p.excel_value for p in result.periods]
    return len(values) == 2 and {any(_close(v, a) for v in values), any(_close(v, b) for v in values)} == {True}


# ---------------------------------------------------------------------------
# Table captions and the sentences that cite them
# ---------------------------------------------------------------------------

# "Tabel 3" on a line of its own with the title on the next ("Kondisi Keuangan dan Akses
# Kredit"), or "Tabel 3. Kondisi Keuangan …" on one line. Not "Tabel 3 menunjukkan …": a title on
# the same line needs the period or colon a caption puts after its number.
_CAPTION_RE = re.compile(r"^\s*(?:lampiran\s+)?tabel\s+(\d+)\s*(?:[.:]\s*(.*))?$", re.IGNORECASE)
_CAPTION_MAX_WORDS = 14
# A sentence's pointers: "(Lampiran Tabel 3)", "(Tabel 3)", "(Lampiran 4 dan 5)", "(Tabel 1 dan Grafik 1)".
_CITED_RE = re.compile(r"\b(lampiran\s+tabel|tabel|lampiran)\s+(\d+(?:\s*(?:,|dan|&)\s*\d+)*)\b",
                       re.IGNORECASE)
# "Grafik 4. Pertumbuhan Ekspor Nonmigas", or "Grafik 4" with its title on the next line.
_CHART_CAPTION_RE = re.compile(r"^\s*grafik\s+(\d+)\s*(?:[.:]\s*(.*))?$", re.IGNORECASE)
# A sentence's pointers to charts: "(Grafik 3)", "(Tabel 1 dan Grafik 1)", "(Grafik 2 dan 3)".
_CHART_CITED_RE = re.compile(r"\bgrafik\s+(\d+(?:\s*(?:,|dan|&)\s*\d+)*)\b", re.IGNORECASE)
# A "Lampiran N" caption of its own (UB's "Lampiran 1. Tabel Uang Beredar …"). Without one, as in
# SKDU, whose appendix is "Tabel 1 … Tabel 11" under one LAMPIRAN heading, "Lampiran 1" is Tabel 1.
_LAMPIRAN_CAPTION_RE = re.compile(r"^\s*lampiran\s+\d+\b", re.IGNORECASE | re.MULTILINE)


# What follows a list-of-tables entry's title: its page number, or a dot leader before it.
_CONTENTS_TAIL_RE = re.compile(r"^(?:\d{1,3}|[.…·\s]{3,}\d{0,3})$")


def _caption_lines(narrative_text: str, caption_re: "re.Pattern" = _CAPTION_RE) -> List[Tuple[int, int, str]]:
    """(offset in the text, number, title) of each 'Tabel N' caption (or 'Grafik N', with
    _CHART_CAPTION_RE), in document order. A list of tables ("Tabel 8 / Perkembangan Ekspor Gas /
    12" in NPI's Daftar Tabel) is not a caption."""
    lines: List[Tuple[int, str]] = []
    offset = 0
    for raw in narrative_text.split("\n"):
        line = raw.strip()
        if line and not _PAGE_MARKER_PATTERN.fullmatch(line):
            lines.append((offset, line))
        offset += len(raw) + 1
    captions: List[Tuple[int, int, str]] = []
    for index, (at, line) in enumerate(lines):
        m = caption_re.match(line)
        if not m or len(line.split()) > _CAPTION_MAX_WORDS:
            continue
        following = []
        for _, after in lines[index + 1:index + 4]:
            if caption_re.match(after):
                break
            following.append(after)
        if any(_CONTENTS_TAIL_RE.match(after) for after in following):
            continue
        title = (m.group(2) or "").strip()
        if not title and following and len(following[0].split()) <= _CAPTION_MAX_WORDS:
            title = following[0]
        captions.append((at, int(m.group(1)), title))
    return captions


def _chart_part(report_text: str) -> List[Tuple[int, str]]:
    """The report's own run of 'Grafik N' captions, up to numbering that starts again."""
    run: List[Tuple[int, str]] = []
    for _, n, title in _caption_lines(report_text, _CHART_CAPTION_RE):
        if run and n < run[-1][0]:
            break
        run.append((n, title))
    return run


def table_captions(narrative_text: str) -> List[Tuple[int, str]]:
    """(number, title) of each 'Tabel N' caption, in document order."""
    return [(n, title) for _, n, title in _caption_lines(narrative_text)]


def _report_part(narrative_text: str) -> Tuple[List[Tuple[int, str]], str]:
    """The report's own run of table captions and the text they belong to. Numbering that starts
    again (NPI: a box's "Tabel 1", the appendix's "TABEL 1 … 10") ends both: past it "Tabel 1"
    is another table, and a sentence there points into its own run."""
    captions = _caption_lines(narrative_text)
    for (_, a, _), (at, b, _) in zip(captions, captions[1:]):
        if b < a:
            run = [(n, title) for start, n, title in captions if start < at]
            return run, narrative_text[:at]
    return [(n, title) for _, n, title in captions], narrative_text


def _cited(sentence: str, lampiran_captions: bool, appendix_apart: bool = False) -> List[str]:
    """The tables a sentence points to, as 'Tabel N' / 'Lampiran N', in the order it names them.

    `appendix_apart`: the appendix numbers its tables from 1 again (NPI), and they are not the
    report's run. A sentence pointing into it ("Lampiran Tabel 9") is then not checked at all."""
    keys: List[str] = []
    for m in _CITED_RE.finditer(sentence):
        if appendix_apart and m.group(1).lower().startswith("lampiran"):
            return []
        kind = "Lampiran" if m.group(1).lower() == "lampiran" and lampiran_captions else "Tabel"
        for n in re.findall(r"\d+", m.group(2)):
            if f"{kind} {int(n)}" not in keys:
                keys.append(f"{kind} {int(n)}")
    return keys


def _missing_numbers(captions: Sequence[Tuple[int, str]]) -> List[int]:
    numbers = {n for n, _ in captions}
    return sorted(set(range(1, max(numbers) + 1)) - numbers)


def _numbering_problems(captions: Sequence[Tuple[int, str]], cited: Dict[int, int],
                        kind: str = "Tabel") -> Optional[str]:
    """What is wrong with the captions' numbering; `cited` counts the sentences citing each number."""
    counts: Dict[int, int] = {}
    for n, _ in captions:
        counts[n] = counts.get(n, 0) + 1
    twice = sorted(n for n, c in counts.items() if c > 1)
    missing = _missing_numbers(captions)
    if not twice and not missing:
        return None
    parts = []
    for n in twice:
        titles = ", ".join(f"'{t}'" for m, t in captions if m == n and t)
        parts.append(f"{kind} {n} dipakai {counts[n]} kali" + (f" ({titles})" if titles else ""))
    for n in missing:
        parts.append(f"tidak ada {kind} {n}" + (f", padahal dirujuk {cited[n]} kalimat" if cited.get(n) else ""))
    return "; ".join(parts)


def _numbering_result(captions: Sequence[Tuple[int, str]], problem: str,
                      kind: str = "Tabel") -> FactVerificationResult:
    listed = ", ".join(str(n) for n, _ in captions)
    word = kind.lower()
    return FactVerificationResult(
        operation="reference", metric_label=f"Nomor {word}", verdict="Refuted",
        context_quote=f"Judul {word} di dokumen: {kind} {listed}", resolved_via=RESOLVED_VIA,
        reasoning=f"Nomor {word} tidak berurutan: {problem} | Dicek dari judul {word} di dokumen, tanpa LLM",
    )


# --- what a table prints ---------------------------------------------------------------------

# A number as a table or a sentence prints it, the Indonesian way: "9.732,8", "7,7", "-0,58".
_PRINTED_RE = re.compile(r"(?<![\w.,])-?\d{1,3}(?:\.\d{3})*(?:,\d+)?(?![\w]|[.,]\d)")
_POINTER_RE = re.compile(r"\b(?:lampiran\s+tabel|tabel|lampiran|grafik|gambar|boks)\s+\d+"
                         r"(?:\s*(?:,|dan|&)\s*\d+)*\b", re.IGNORECASE)


_CHART_POINTER_RE = re.compile(r"\b(?:grafik|gambar)\s+\d+", re.IGNORECASE)
_CURRENCY_RE = re.compile(r"\b(?:Rp|USD|US\$)(?=\s?\d)", re.IGNORECASE)


def _key(printed: str) -> str:
    """'9.732,8' → '9732,8': the digits as printed, sign and thousands dots left out."""
    return printed.lstrip("-").replace(".", "")


def _printed(table_text: str) -> Set[str]:
    return {_key(m) for m in _PRINTED_RE.findall(table_text)}


def _sentence_figures(sentence: str) -> Dict[str, str]:
    """The sentence's figures (key → as printed), without its pointers ("Tabel 3", "Grafik 1")
    and its years. A level written after its currency ("Rp10.448,1 triliun") counts: it is the
    figure a table is least likely to print by chance."""
    text = _CURRENCY_RE.sub(" ", _POINTER_RE.sub(" ", sentence))
    figures: Dict[str, str] = {}
    for m in _PRINTED_RE.findall(text):
        if not re.fullmatch(r"(?:19|20)\d\d", m):
            figures.setdefault(_key(m), m.lstrip("-"))
    return figures


def _distinctive(figure: str) -> bool:
    """A figure unlikely to be printed in a table by chance: one with decimals, or a level of
    four digits or more. "3" or "10" turns up in every table."""
    return "," in figure or len(figure) >= 4


def _rounds_to(figure: str, printed: Set[str]) -> bool:
    """The table prints `figure`, or a longer number the sentence rounded to it (1,58 → 1,6)."""
    if figure in printed:
        return True
    decimals = len(figure.split(",")[1]) if "," in figure else 0
    try:
        target = Decimal(figure.replace(",", "."))
    except InvalidOperation:
        return False
    step = Decimal(1).scaleb(-decimals)
    for p in printed:
        if "," not in p or len(p.split(",")[1]) <= decimals:
            continue
        if Decimal(p.replace(",", ".")).quantize(step, rounding=ROUND_HALF_UP) == target:
            return True
    return False


# Two figures of the sentence printed in another table: one shared figure could be chance (a
# "2,6%" sits in many tables), two in the same table is where they come from.
_MIN_FIGURES_ELSEWHERE = 2


def _printed_elsewhere(cited: List[str], figures: List[str], printed: Dict[str, Set[str]]) -> Optional[str]:
    """The one other table the sentence's figures come from: the cited tables print none of them,
    or only some ("10,3" turning up in NPI's Tabel 4) while that table prints every one."""
    in_cited = sum(any(_rounds_to(f, printed[c]) for c in cited) for f in figures)
    if in_cited == len(figures):
        return None
    distinctive = [f for f in figures if _distinctive(f)]
    scored = sorted(((sum(f in found for f in distinctive), key) for key, found in printed.items()
                     if key not in cited), reverse=True)
    if not scored or scored[0][0] < _MIN_FIGURES_ELSEWHERE or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    if in_cited and scored[0][0] < len(figures):
        return None
    return scored[0][1]


def _reference_result(sentence: str, page: Optional[int], cited: List[str], problem: str,
                      checked_from: str) -> FactVerificationResult:
    return FactVerificationResult(
        operation="reference", metric_label=f"Rujukan {' dan '.join(cited)}", verdict="Refuted",
        context_quote=sentence, page_number=page, resolved_via=RESOLVED_VIA,
        reasoning=f"{problem} | Dicek dari {checked_from}, tanpa LLM",
    )


# ---------------------------------------------------------------------------
# All of it
# ---------------------------------------------------------------------------

# At least this many captions before a cited number may be called missing: a document whose
# captions the reader did not see (a scanned PDF, a table pasted as one picture) has none or few.
_MIN_CAPTIONS = 3


@dataclass
class Pointer:
    """A sentence's pointer to the report's tables, and what the code made of it.

    `status`: "absent" — a cited Tabel N has no caption; "elsewhere" — the cited tables print none
    (or only some) of the sentence's figures and one other table prints them; "holds" — the cited
    tables print its figures; "open" — the numbers settle nothing (none in the sentence, a cited
    picture unread, the figures in several tables or in none: wrong, derived, or rounded away)."""
    sentence: str
    page: Optional[int]
    cited: List[str]
    status: str = "open"
    problem: str = ""
    before: str = ""    # the sentences just before it: "Hal tersebut tecermin dari SBT …" needs them
    # Its pointers to charts ('Grafik 3'). A chart's content is never read: "absent" when no
    # caption carries the number, else "open" — judged from the chart's title alone.
    charts: List[str] = field(default_factory=list)
    chart_status: str = "open"
    chart_problem: str = ""


# How many sentences before a pointer's own come with it, for a reader judging its subject.
_CONTEXT_SENTENCES = 2
# "Lampiran 4. Kredit yang Disalurkan …": an appendix table with its own caption (UB).
_LAMPIRAN_TITLE_RE = re.compile(r"^\s*lampiran\s+(\d+)\s*[.:]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class _Pointers:
    captions: List[Tuple[int, str]]
    titles: Dict[str, str]           # 'Tabel 3' / 'Lampiran 4' / 'Grafik 2' → its title, the report's own run
    pointers: List[Pointer]
    numbering_problem: Optional[str]
    charts: List[Tuple[int, str]] = field(default_factory=list)
    chart_numbering_problem: Optional[str] = None


def _cited_charts(sentence: str) -> List[str]:
    keys: List[str] = []
    for m in _CHART_CITED_RE.finditer(sentence):
        for n in re.findall(r"\d+", m.group(1)):
            if f"Grafik {int(n)}" not in keys:
                keys.append(f"Grafik {int(n)}")
    return keys


def _count_citations(pointers: List["Pointer"], kind: str) -> Dict[int, int]:
    counted: Dict[int, int] = {}
    for p in pointers:
        for key in (p.charts if kind == "Grafik" else p.cited):
            if key.startswith(f"{kind} "):
                n = int(key.split()[1])
                counted[n] = counted.get(n, 0) + 1
    return counted


def _pointers(narrative_text: str, table_texts: Optional[Dict[str, str]]) -> _Pointers:
    # A PDF's text layer keeps few of its captions whole (PPKom Tw IV-2025: 3 of 18), so its
    # numbering and pointers would read as gaps and missing tables: a PDF's are not checked.
    if table_texts is None:
        return _Pointers([], {}, [], None)
    # The report's own tables and the sentences before any numbering that starts again
    # (word_extraction keeps the same run of pictures).
    captions, report_text = _report_part(narrative_text)
    numbers = [n for n, _ in captions]
    titles = {f"Tabel {n}": title for n, title in captions}
    for m in _LAMPIRAN_TITLE_RE.finditer(report_text):
        if len(m.group(0).split()) <= _CAPTION_MAX_WORDS + 6:
            titles.setdefault(f"Lampiran {int(m.group(1))}", m.group(2))
    lampiran_captions = bool(_LAMPIRAN_CAPTION_RE.search(report_text)) or any(
        key.startswith("Lampiran") for key in (table_texts or {}))
    appendix_apart = report_text is not narrative_text
    charts = _chart_part(report_text)
    titles.update({f"Grafik {n}": title for n, title in charts})
    sentences = _sentences(report_text)
    pointers = [Pointer(sentence, page, _cited(sentence, lampiran_captions, appendix_apart),
                        before=" ".join(s for s, at in sentences[max(0, i - _CONTEXT_SENTENCES):i] if at == page),
                        charts=_cited_charts(sentence))
                for i, (sentence, page) in enumerate(sentences)]
    pointers = [p for p in pointers if p.cited or p.charts]

    # Against the captions: the numbering, and a pointer to a number no caption carries.
    skipped: Set[int] = set()
    numbering_problem = None
    enough_captions = len(captions) >= _MIN_CAPTIONS
    if enough_captions:
        numbering_problem = _numbering_problems(captions, _count_citations(pointers, "Tabel"))
        # A pointer to a number the numbering skips is that card's finding, not one per sentence.
        skipped = set(_missing_numbers(captions))
    chart_numbering_problem = None
    if len(charts) >= _MIN_CAPTIONS:
        chart_numbering_problem = _numbering_problems(charts, _count_citations(pointers, "Grafik"), "Grafik")
        charted = {n for n, _ in charts}
        chart_skipped = set(_missing_numbers(charts))
        for p in pointers:
            absent = [key for key in p.charts if int(key.split()[1]) not in charted
                      and int(key.split()[1]) not in chart_skipped]
            if absent:
                p.chart_status = "absent"
                p.chart_problem = f"Kalimat merujuk {absent[0]}, tetapi tidak ada judul {absent[0]} di dokumen"

    printed = {key: _printed(text) for key, text in (table_texts or {}).items()}
    for p in pointers:
        if not p.cited:
            continue
        absent = [key for key in p.cited if key.startswith("Tabel ")
                  and int(key.split()[1]) not in numbers and int(key.split()[1]) not in skipped]
        if enough_captions and absent:
            p.status = "absent"
            p.problem = f"Kalimat merujuk {absent[0]}, tetapi tidak ada judul {absent[0]} di dokumen"
            continue
        # Against what the tables print: every cited table has to be one whose picture was read.
        figures = _sentence_figures(p.sentence)
        if not figures or any(key not in printed for key in p.cited):
            continue
        # The figures prove the pointer unless another table prints more of them: NPI's cited
        # Tabel 4 prints the "10,3" of a sentence whose "17,9" and "10,3" are both in Tabel 5. A tie
        # holds (SKDU prints a LU's SBT in several tables).
        in_cited = sum(any(_rounds_to(f, printed[key]) for key in p.cited) for f in figures)
        in_other = max((sum(_rounds_to(f, found) for f in figures) for key, found in printed.items()
                        if key not in p.cited), default=0)
        if in_cited and in_cited >= in_other:
            p.status = "holds"
        # "(Tabel 1 dan Grafik 1)": the figures the table lacks may be the chart's (UB Juli 2026's
        # Tabel 1 prints the levels, Grafik 1 the growth), so the code cannot call them misplaced.
        other = None if _CHART_POINTER_RE.search(p.sentence) else _printed_elsewhere(p.cited, list(figures), printed)
        if other is not None:
            shown = ", ".join(as_printed for key, as_printed in figures.items() if key in printed[other])
            p.status = "elsewhere"
            p.problem = (f"Kalimat merujuk {' dan '.join(p.cited)}, tetapi angkanya ({shown}) tidak ada "
                         f"di tabel itu; angka tersebut ada di {other}")
    return _Pointers(captions, titles, pointers, numbering_problem, charts, chart_numbering_problem)


def open_pointers(narrative_text: str, table_texts: Optional[Dict[str, str]] = None
                  ) -> Tuple[Dict[str, str], List[Tuple[Pointer, List[str]]]]:
    """The report's tables and charts (key → title) and the pointers left for a reader who goes by
    the sentence's subject, each as (pointer, the cited keys to judge): its tables when the numbers
    settle nothing, its charts always (a chart is never read), each only when every cited key has
    a caption."""
    found = _pointers(narrative_text, table_texts)
    items: List[Tuple[Pointer, List[str]]] = []
    for p in found.pointers:
        if p.cited and p.status == "open" and all(key in found.titles for key in p.cited):
            items.append((p, p.cited))
        if p.charts and p.chart_status == "open" and all(key in found.titles for key in p.charts):
            items.append((p, p.charts))
    return found.titles, items


def chart_titles(narrative_text: str) -> Dict[str, str]:
    """The report's own charts ('Grafik 4' → its title), read from their captions."""
    return {f"Grafik {n}": title for n, title in _chart_part(_report_part(narrative_text)[1])}


def check_document(narrative_text: str, results: List[FactVerificationResult],
                   table_texts: Optional[Dict[str, str]] = None) -> List[FactVerificationResult]:
    """`results` with the document's own mistakes added as Tidak Sesuai cards (see the module
    docstring). A trend claim that read a wrongly worded comparison the way the numbers go is
    replaced by the comparison's card, so the sentence is not shown as both.

    `table_texts`: what each table of a Word document prints ('Tabel 3' → its picture's text,
    word_extraction.WordDocument.table_texts; empty when no picture could be read). None, for a
    PDF, leaves the table numbering and pointers unchecked: only the comparisons are."""
    out = list(results)
    for sentence, page in _sentences(narrative_text):
        for a, b, word in _wrong_comparisons(sentence):
            same = [r for r in out if _is_same_comparison(r, sentence, a, b)]
            if any(r.verdict == "Refuted" for r in same):
                continue
            out = [r for r in out if r not in same]
            out.append(_comparison_result(sentence, page, a, b, word))

    found = _pointers(narrative_text, table_texts)
    if found.numbering_problem:
        out.append(_numbering_result(found.captions, found.numbering_problem))
    if found.chart_numbering_problem:
        out.append(_numbering_result(found.charts, found.chart_numbering_problem, "Grafik"))
    for p in found.pointers:
        if p.cited and p.status == "absent":
            out.append(_reference_result(p.sentence, p.page, p.cited, p.problem, "judul tabel di dokumen"))
        elif p.cited and p.status == "elsewhere":
            out.append(_reference_result(p.sentence, p.page, p.cited, p.problem, "isi tabel di dokumen"))
        if p.chart_status == "absent":
            out.append(_reference_result(p.sentence, p.page, p.charts, p.chart_problem, "judul grafik di dokumen"))
    return out
