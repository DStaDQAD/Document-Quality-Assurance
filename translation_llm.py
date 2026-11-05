"""Cek Terjemahan, the plain flow (spike, 2026-11-05): the model pairs the two texts and compares them.

1. Each document's text, line by line, as read from the file. No rule says what a paragraph, a
   heading, a page number or a table of contents is.
2. The model pairs the lines of the report's text (paragraphs and titles) and leaves the rest out,
   window by window: a long report goes in parts of about CHUNK_CHARS Indonesian characters, each
   with the English lines around the same place.
3. The model compares each pair's meaning (paragraphs) or substance (titles). Code only checks
   that each number of a pair is printed on both sides.
"""
import asyncio
import bisect
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from schemas import TranslationCheckResponse, TranslationFinding, TranslationPair
from publication_parsers import translation_terms
from translation_check import _PAGE_MARKER, MeaningFindings, Pair, _batches, number_finding

logger = logging.getLogger("fact-checker")

CHUNK_CHARS = 6000          # Indonesian text per pairing call
CHUNK_LINES = 80            # and at most this many Indonesian lines (a contents list is many short ones)
MARGIN_CHARS = 3000         # English text added on each side of the matching place
CALL_TIMEOUT_S = 120        # one model call, before it is asked again (then reported unchecked)


@dataclass
class Line:
    text: str
    page: Optional[int]


def lines_of(narrative_text: str) -> List[Line]:
    """The text's non-empty lines, each with the page marker last seen above it (PDF)."""
    out: List[Line] = []
    page = None
    for raw in narrative_text.splitlines():
        line = raw.strip()
        marker = _PAGE_MARKER.match(line)
        if marker:
            page = int(marker.group(1))
        elif line:
            out.append(Line(line, page))
    return out


# ── Pairing ──────────────────────────────────────────────────────────────────────────────────────

class Group(BaseModel):
    id_lines: List[int] = Field(description="The I numbers of the Indonesian line(s), consecutive")
    en_lines: List[int] = Field(description="The E numbers of the English line(s) that translate them, consecutive")
    kind: Literal["paragraph", "heading"] = Field(description="heading for a section, chart or table title")


class WindowPairs(BaseModel):
    pairs: List[Group] = Field(default_factory=list)
    id_only: List[int] = Field(default_factory=list,
                               description="I numbers of report text with no English counterpart")
    en_only: List[int] = Field(default_factory=list,
                               description="E numbers of report text with no Indonesian counterpart")
    skipped: List[int] = Field(default_factory=list,
                               description="I numbers of lines that are not the report's text")


PAIR_PROMPT = """You align an Indonesian statistical publication (Bank Indonesia) with its English version, which is meant to be a direct translation. You get a run of numbered Indonesian lines (I…) and a run of numbered English lines (E…) covering the same part of the document; the English run starts a little earlier and ends a little later.

Pair each Indonesian line of the report's text with the English line or lines that translate it. The report's text is its narrative paragraphs, section titles, chart and table titles, notes under tables and source lines. A paragraph or title split over several lines on one side is one group of consecutive lines. Leave out what is not the report's text: cover and contact details, the table of contents and the lists of tables and charts with their page numbers, page numbers, a stand-alone label such as "Grafik 1" or "Chart 1", running headers and footers.

The two versions follow the same order, except that a paragraph may sit on the other side of a chart or table. A translation can carry a wrong number, period or direction and still be the counterpart: pair on what the text is about. kind is heading for a title, paragraph otherwise.

Every Indonesian line goes in exactly one place: a group, id_only or skipped. Put a line that is not the report's text in skipped. Put a line of the Indonesian report text with no English counterpart in id_only. Put a line of English report text in en_only only when it has no Indonesian counterpart AND sits among the English lines you paired, not before the first or after the last of them."""

_PAIR_PROMPT = ChatPromptTemplate.from_messages([("system", PAIR_PROMPT), ("human", "{lines}")])


def _windows(id_lines: List[Line], en_lines: List[Line]) -> List[Tuple[range, range]]:
    """(Indonesian lines, English lines) per call: the Indonesian text in parts of CHUNK_CHARS, each
    with the English lines at the same share of the English text, MARGIN_CHARS wider each side."""
    def cumulative(lines):
        out, total = [0], 0
        for line in lines:
            total += len(line.text) + 1
            out.append(total)
        return out
    id_cum, en_cum = cumulative(id_lines), cumulative(en_lines)
    windows = []
    start = 0
    while start < len(id_lines):
        end = start
        while end < len(id_lines) and (end == start or (id_cum[end] - id_cum[start] < CHUNK_CHARS and end - start < CHUNK_LINES)):
            end += 1
        lo = id_cum[start] / id_cum[-1] * en_cum[-1] - MARGIN_CHARS
        hi = id_cum[end] / id_cum[-1] * en_cum[-1] + MARGIN_CHARS
        en_start = max(0, bisect.bisect_right(en_cum, lo) - 1)
        en_end = min(len(en_lines), bisect.bisect_left(en_cum, hi))
        windows.append((range(start, end), range(en_start, en_end)))
        start = end
    return windows


async def _twice(make_call):
    """The call's answer, asked again once when it fails or comes back empty: the review model
    once returned no answer for an NPI window (2026-11-05)."""
    for attempt in (1, 2):
        try:
            # A cap on the whole call: the provider's stall timer only fires on silence, and a
            # reasoning call that keeps streaming held one UB Juni run for over five minutes.
            answer = await asyncio.wait_for(make_call(), CALL_TIMEOUT_S)
            if answer is not None:
                return answer
            logger.warning("Translation LLM call returned nothing (attempt %d)", attempt)
        except Exception as exc:  # noqa: BLE001 - asked again, then reported unchecked
            logger.warning("Translation LLM call failed (attempt %d): %s", attempt, exc)
            if attempt == 2:
                raise
    raise RuntimeError("the model returned no answer twice")


async def _pair_window(ids: range, ens: range, id_lines: List[Line], en_lines: List[Line], llm) -> WindowPairs:
    text = ("INDONESIAN\n" + "\n".join(f"I{i + 1}: {id_lines[i].text}" for i in ids)
            + "\n\nENGLISH\n" + "\n".join(f"E{j + 1}: {en_lines[j].text}" for j in ens))
    # Function calling, not the default JSON schema: on NPI Tw II-2026 a window took 3 s this way and
    # over 240 s (the client busy all along) the other.
    chain = _PAIR_PROMPT | llm.with_structured_output(WindowPairs, method="function_calling")
    return await _twice(lambda: chain.ainvoke({"lines": text}))


@dataclass
class _Item:
    ids: List[int]          # 0-based line positions
    ens: List[int]
    kind: str


def _merge(windows: List[Tuple[range, range]], answers) -> Tuple[List[_Item], int]:
    """The windows' groups in Indonesian reading order. A group numbered outside its window, or one
    that reuses a line of an earlier group, is dropped; English lines left unpaired are English-only when
    some window said so."""
    groups: List[_Item] = []
    id_only, en_only = set(), set()
    for (ids, ens), answer in zip(windows, answers):
        if isinstance(answer, BaseException):
            logger.warning("Translation pairing window failed: %s", answer)
            continue
        for g in answer.pairs:
            gi, ge = sorted(n - 1 for n in g.id_lines), sorted(n - 1 for n in g.en_lines)
            if gi and ge and all(i in ids for i in gi) and all(j in ens for j in ge):
                groups.append(_Item(gi, ge, g.kind))
        id_only |= {n - 1 for n in answer.id_only if n - 1 in ids} - {n - 1 for n in answer.skipped}
        en_only |= {n - 1 for n in answer.en_only if n - 1 in ens}
    kept: List[_Item] = []
    used_id, used_en = set(), set()
    dropped = 0
    for g in sorted(groups, key=lambda g: (g.ids[0], g.ens[0])):
        # A group may cross another: NPI Tw II-2026 prints a paragraph before its chart in English and
        # after it in Indonesian, and dropping the crossing group lost the paragraph unchecked.
        if used_id & set(g.ids) or used_en & set(g.ens):
            dropped += 1
            continue
        kept.append(g)
        used_id |= set(g.ids)
        used_en |= set(g.ens)
    items = kept + [_Item([i], [], "paragraph") for i in sorted(id_only - used_id)]
    items += [_Item([], [j], "paragraph") for j in sorted(en_only - used_en)]

    paired_en = sorted((g.ens[0], g.ids[0]) for g in kept)

    def key(item: _Item):
        if item.ids:
            return (item.ids[0], 0)
        k = bisect.bisect_left(paired_en, (item.ens[0], -1))
        return (paired_en[k - 1][1] if k else -1, 1)
    return sorted(items, key=key), dropped


# ── Comparing ────────────────────────────────────────────────────────────────────────────────────
# What is not a difference is said once, here, with the reasons the first runs gave (2026-11-05),
# and the publication's own terms come from its notes file (translation section).

NOT_A_DIFFERENCE = """Not a difference, never report it:
- the same fact worded another way ("penurunan tertahan oleh faktor harga" / "a deeper decline was offset by improving prices"; "peningkatan arus masuk neto" / "a higher net inflow");
- a generic word added or dropped: tingkat, kinerja, perkembangan, pertumbuhan, posisi, kondisi, total, level, development, performance;
- the same items named in another order, each still with its own number or role;
- the same thing under another usual name, including those listed under TERMS;
- number formatting (10.448,1 = 10,448.1) and a unit written another way (persen = %, miliar dolar AS = USD billion).
When you would write that there is no difference, or that both versions say the same, leave the pair out."""

SYSTEM_PROMPT = """You compare paragraphs of an Indonesian statistical publication (Bank Indonesia) with their English version, which is meant to be a direct translation. Each PAIR holds one Indonesian text (ID) and its English counterpart (EN).

Report real differences in content only:
- For EVERY number, check that in both versions it refers to the same subject or component, the same period and the same direction. Numbers swapped between items ("masing-masing" / "respectively") are a difference.
- Direction: naik/meningkat/tumbuh/akselerasi against fell/decreased/slowed, and the reverse.
- Period: month, quarter, year, yoy against mtm, "bulan sebelumnya" against a named month that is not the previous one.
- Subject: a different indicator, component, sector, region or instrument (time deposits for demand deposits), or an item of a list dropped, added or replaced.
- Scale: triliun = trillion, miliar = billion, juta = million.

{not_a_difference}
{terms}
For each difference give the PAIR number, the exact differing fragment of each text, and one sentence in Indonesian saying what differs, not which version is right. An empty list is the normal answer."""

HEADING_PAIRS_PROMPT = """You compare titles of an Indonesian statistical publication (Bank Indonesia) with their English counterparts: section, sector, chart and table titles. Each PAIR holds one Indonesian title (ID) and its English counterpart (EN).

Report a pair ONLY when the two differ in substance: a different sector, industry, component, indicator, instrument or region; an item of a named list dropped, added or replaced ("Pengadaan Listrik dan Gas" against "Electricity Supply" drops gas); a different period or direction.

Also not a difference: a title naming its part more broadly or more narrowly than its counterpart; generic words such as Lapangan Usaha, LU, Sektor, Sector, Growth.
{not_a_difference}
{terms}
For each difference give the PAIR number, the differing fragment of each title and one sentence in Indonesian saying what differs. An empty list is the normal answer."""

_PROMPT = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", "{pairs}")])
_HEADING_PAIRS_PROMPT = ChatPromptTemplate.from_messages([("system", HEADING_PAIRS_PROMPT), ("human", "{pairs}")])
# An explanation that says there is no difference is not a finding, whatever the model was told.
_NO_DIFFERENCE = re.compile(
    r"(?i)tidak ada perbedaan|tidak berbeda|kedua versi (sama|menyebut hal yang sama)|no difference")


def _terms_block(publication: str) -> str:
    terms = translation_terms(publication) if publication else []
    if not terms:
        return ""
    return ("\nTERMS (the publication's usual Indonesian terms and their English equivalents):\n"
            + "\n".join(f"- {t}" for t in terms) + "\n")


async def _compare(batch: List[Pair], llm, prompt, terms: str):
    text = "\n\n".join(f"[PAIR {p.index}]\nID: {p.id_text}\nEN: {p.en_text}" for p in batch)
    chain = prompt | llm.with_structured_output(MeaningFindings, method="function_calling")
    answer = await _twice(lambda: chain.ainvoke({"pairs": text, "not_a_difference": NOT_A_DIFFERENCE,
                                                 "terms": terms}))
    return answer.findings


async def check_translation_llm(id_text: str, en_text: str, llm, *, document: str, id_filename: str,
                                en_filename: str, id_is_word: bool = False, en_is_word: bool = False,
                                max_chars: int = 6000, heading_llm=None, pair_llm=None,
                                publication: str = "") -> TranslationCheckResponse:
    """The English text against the Indonesian one: lines paired by `pair_llm` (the review model:
    the plain one did not keep to its instructions on long reports), numbers checked by code,
    meaning by `llm`, titles by `heading_llm`, with the publication's terms as context."""
    id_lines, en_lines = lines_of(id_text), lines_of(en_text)
    for lines, side in ((id_lines, "versi Indonesia"), (en_lines, "versi Inggris")):
        if not lines:
            raise ValueError(f"Tidak ada narasi yang terbaca dari dokumen {side}. Unggah versi Word (.docx) "
                             "atau PDF yang teksnya bisa disalin.")
    windows = _windows(id_lines, en_lines)
    answers = await asyncio.gather(*(_pair_window(i, e, id_lines, en_lines, pair_llm or llm) for i, e in windows),
                                   return_exceptions=True)
    failed = sum(isinstance(a, BaseException) for a in answers)
    items, dropped = _merge(windows, answers)
    if dropped:
        logger.info("Translation pairing: %d groups reusing a line dropped", dropped)

    pairs: List[Pair] = []
    out: List[TranslationPair] = []
    id_section = en_section = None
    for n, item in enumerate(items):
        id_t = " ".join(id_lines[i].text for i in item.ids) or None
        en_t = " ".join(en_lines[j].text for j in item.ens) or None
        if item.kind == "heading":
            id_section, en_section = id_t or id_section, en_t or en_section
        pairs.append(Pair(n, id_t, en_t))
        out.append(TranslationPair(
            index=n, id_text=id_t, en_text=en_t,
            id_page=None if id_is_word or not item.ids else id_lines[item.ids[0]].page,
            en_page=None if en_is_word or not item.ens else en_lines[item.ens[0]].page,
            id_section=("Judul" if item.kind == "heading" else id_section) if id_is_word and item.ids else None,
            en_section=("Judul" if item.kind == "heading" else en_section) if en_is_word and item.ens else None))
    # The same text printed twice on both sides (a note under two tables) is checked and shown once.
    seen = set()
    first: List[Pair] = []
    for pair in pairs:
        key = (pair.id_text, pair.en_text)
        if key not in seen:
            seen.add(key)
            first.append(pair)
            finding = number_finding(pair)
            if finding is not None:
                out[pair.index].findings.append(finding)
    kinds = {n: item.kind for n, item in enumerate(items)}
    para_batches = _batches([p for p in first if kinds[p.index] == "paragraph"], max_chars)
    head_batches = _batches([p for p in first if kinds[p.index] == "heading"], max_chars)
    terms = _terms_block(publication)
    calls = ([_compare(b, llm, _PROMPT, terms) for b in para_batches]
             + [_compare(b, heading_llm or llm, _HEADING_PAIRS_PROMPT, terms) for b in head_batches])
    results = await asyncio.gather(*calls, return_exceptions=True)
    for batch, answer in zip(para_batches + head_batches, results):
        if isinstance(answer, BaseException):
            logger.warning("Translation meaning check failed for pairs %s: %s", [p.index for p in batch], answer)
            for pair in batch:
                out[pair.index].checked = False
            continue
        inside = {p.index for p in batch}
        for found in answer:
            if found.pair in inside and not _NO_DIFFERENCE.search(found.explanation):
                out[found.pair].findings.append(TranslationFinding(
                    kind="makna", explanation=found.explanation, id_quote=found.id_quote, en_quote=found.en_quote))
    if failed:
        out.append(TranslationPair(index=len(out), id_text=f"{failed} bagian dokumen gagal dipasangkan",
                                   checked=False))
    findings = [f for item in out for f in item.findings]
    return TranslationCheckResponse(
        document=document, id_filename=id_filename, en_filename=en_filename, pairs=out,
        n_number=sum(f.kind == "angka" for f in findings), n_meaning=sum(f.kind == "makna" for f in findings),
        n_unchecked=sum(not item.checked for item in out))
