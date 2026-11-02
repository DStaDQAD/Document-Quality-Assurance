"""Checks a report's text against itself, with no table and no LLM.

Three mistakes no workbook can show, all planted by the mentor in SKDU Tw III-2026:
  - a sentence that compares two of its own numbers the wrong way round ("22,68%, lebih rendah
    dibandingkan 22,34%") — the numbers are right, the word is not;
  - a sentence citing the wrong appendix table ("akses kredit … (Lampiran Tabel 1)", the
    Kegiatan Usaha table, while its figures are Tabel 3's);
  - the appendix's own numbering ("Tabel 10" printed twice, no Tabel 9).

Each finding is one Tidak Sesuai card (operation "comparison" or "reference"), added after the
claims are judged, so it never changes how a claim is read.
"""
import re
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
# "(Lampiran Tabel 3)", "Tabel 3": a sentence's pointer to the table it reads.
_CITED_RE = re.compile(r"\b(?:lampiran\s+)?tabel\s+(\d+)\b", re.IGNORECASE)
_TITLE_FILLER = {"tabel", "dan", "atau", "yang", "per", "kondisi", "perkembangan", "lampiran", "menurut",
                 "lapangan", "usaha"}


def table_captions(narrative_text: str) -> List[Tuple[int, str]]:
    """(number, title) of each 'Tabel N' caption, in document order."""
    lines = [line.strip() for line in narrative_text.split("\n")]
    lines = [line for line in lines if line and not _PAGE_MARKER_PATTERN.fullmatch(line)]
    captions: List[Tuple[int, str]] = []
    for index, line in enumerate(lines):
        m = _CAPTION_RE.match(line)
        if not m or len(line.split()) > _CAPTION_MAX_WORDS:
            continue
        title = (m.group(2) or "").strip()
        if not title and index + 1 < len(lines):
            following = lines[index + 1]
            if not _CAPTION_RE.match(following) and len(following.split()) <= _CAPTION_MAX_WORDS:
                title = following
        captions.append((int(m.group(1)), title))
    return captions


def _words(text: str) -> Set[str]:
    words = {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) >= 3}
    return words - _TITLE_FILLER


def _missing_numbers(captions: Sequence[Tuple[int, str]]) -> List[int]:
    numbers = {n for n, _ in captions}
    return sorted(set(range(1, max(numbers) + 1)) - numbers)


def _numbering_problems(captions: Sequence[Tuple[int, str]], cited: Dict[int, int]) -> Optional[str]:
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
        parts.append(f"Tabel {n} dipakai {counts[n]} kali" + (f" ({titles})" if titles else ""))
    for n in missing:
        parts.append(f"tidak ada Tabel {n}" + (f", padahal dirujuk {cited[n]} kalimat" if cited.get(n) else ""))
    return "; ".join(parts)


def _numbering_result(captions: Sequence[Tuple[int, str]], problem: str) -> FactVerificationResult:
    listed = ", ".join(str(n) for n, _ in captions)
    return FactVerificationResult(
        operation="reference", metric_label="Nomor tabel", verdict="Refuted",
        context_quote=f"Judul tabel di dokumen: Tabel {listed}", resolved_via=RESOLVED_VIA,
        reasoning=f"Nomor tabel tidak berurutan: {problem} | Dicek dari judul tabel di dokumen, tanpa LLM",
    )


def _source_words(label: str, sources: Sequence) -> Set[str]:
    src = next((s for s in sources if s.label == label), None)
    if src is None:
        return set()
    return _words(f"{src.sheet} {src.table.title or ''}")


def _best_caption(words: Set[str], titles: Dict[int, Set[str]]) -> Optional[int]:
    """The one caption whose title shares the most words with a table, when one alone does."""
    scored = sorted(((len(words & t), n) for n, t in titles.items()), reverse=True)
    if not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1]


def _reference_problem(cited: int, labels: Set[str], sources: Sequence,
                       titles: Dict[int, Set[str]], caption_title: Dict[int, str]) -> Optional[str]:
    if cited not in titles:
        return f"Kalimat merujuk Tabel {cited}, tetapi tidak ada judul Tabel {cited} di dokumen"
    tables = {label: _source_words(label, sources) for label in labels}
    # Any figure of the sentence read from a table that fits the cited caption: the pointer holds.
    if any(words & titles[cited] for words in tables.values()):
        return None
    fits = {_best_caption(words, titles) for words in tables.values()}
    if len(fits) != 1 or None in fits:
        return None
    other = fits.pop()
    if other == cited:
        return None
    return (f"Kalimat merujuk Tabel {cited} ('{caption_title[cited]}'), tetapi angkanya dari tabel "
            f"'{next(iter(labels)).split(' / ')[-1]}', yang sesuai dengan Tabel {other} ('{caption_title[other]}')")


def _reference_result(sentence: str, page: Optional[int], cited: int, problem: str) -> FactVerificationResult:
    return FactVerificationResult(
        operation="reference", metric_label=f"Rujukan Tabel {cited}", verdict="Refuted",
        context_quote=sentence, page_number=page, resolved_via=RESOLVED_VIA,
        reasoning=f"{problem} | Dicek dari judul tabel di dokumen dan tabel Excel yang menjawab kalimat ini",
    )


# ---------------------------------------------------------------------------
# All of it
# ---------------------------------------------------------------------------

# At least this many captions before a cited number may be called missing: a document whose
# captions the reader did not see (a scanned PDF, a table pasted as one picture) has none or few.
_MIN_CAPTIONS = 3


def check_document(narrative_text: str, results: List[FactVerificationResult],
                   sources: Sequence) -> List[FactVerificationResult]:
    """`results` with the document's own mistakes added as Tidak Sesuai cards (see the module
    docstring). A trend claim that read a wrongly worded comparison the way the numbers go is
    replaced by the comparison's card, so the sentence is not shown as both."""
    out = list(results)
    sentences = _sentences(narrative_text)

    for sentence, page in sentences:
        for a, b, word in _wrong_comparisons(sentence):
            same = [r for r in out if _is_same_comparison(r, sentence, a, b)]
            if any(r.verdict == "Refuted" for r in same):
                continue
            out = [r for r in out if r not in same]
            out.append(_comparison_result(sentence, page, a, b, word))

    captions = table_captions(narrative_text)
    if len(captions) < _MIN_CAPTIONS:
        return out
    # Numbering that starts again (NPI: the body's tables, each box's, the appendix's) or a
    # two-column layout read across (1, 7, 2, 8, …) leaves "Tabel 3" naming several tables.
    numbers = [n for n, _ in captions]
    if any(b < a for a, b in zip(numbers, numbers[1:])):
        return out
    cited_in: List[Tuple[str, Optional[int], int]] = []
    for sentence, page in sentences:
        cited = {int(n) for n in _CITED_RE.findall(sentence)}
        if len(cited) == 1:
            cited_in.append((sentence, page, cited.pop()))
    citations: Dict[int, int] = {}
    for _, _, number in cited_in:
        citations[number] = citations.get(number, 0) + 1
    problem = _numbering_problems(captions, citations)
    if problem:
        out.append(_numbering_result(captions, problem))
    # A pointer to a number the numbering skips is that card's finding, not one per sentence.
    skipped = set(_missing_numbers(captions))
    titles: Dict[int, Set[str]] = {}
    caption_title: Dict[int, str] = {}
    for n, title in captions:
        titles.setdefault(n, set()).update(_words(title))
        caption_title.setdefault(n, title)
    by_sentence: Dict[str, Set[str]] = {}
    for r in results:
        if r.verdict != "Inconclusive" and r.matched_excel_source and r.resolved_via != RESOLVED_VIA:
            by_sentence.setdefault(_normalize_ws(r.context_quote), set()).add(r.matched_excel_source)
    for sentence, page, number in cited_in:
        labels = by_sentence.get(sentence, set())
        if number in skipped or (number in titles and not labels):
            continue
        found = _reference_problem(number, labels, sources, titles, caption_title)
        if found:
            out.append(_reference_result(sentence, page, number, found))
    return out
