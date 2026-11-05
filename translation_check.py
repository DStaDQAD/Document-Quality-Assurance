"""Cek Terjemahan: an English publication checked against its Indonesian version.

The English text is a direct translation, paragraph by paragraph, so the two are paired in order
and every number of a paragraph must appear in its pair. That part is code: the numbers are read
in each language's format and compared as sets, whatever order the sentence puts them in. What a
number is attached to, and the meaning around it (direction, period, subject, unit), is left to
the LLM. Differences are flagged; which version is wrong is the reviewer's call.
"""
import asyncio
import difflib
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from schemas import TranslationCheckResponse, TranslationFinding, TranslationPair

logger = logging.getLogger("fact-checker")

_PAGE_MARKER = re.compile(r"^\[== Halaman (\d+) ==\]$")
# A paragraph of fewer than three words and no figure but a year carries nothing to compare: the
# stray "s" the UB English reports hold between two paragraphs, a date line ("Agustus 2026", which
# ID prints after the title and EN before it), "Keterangan:" under a table.
_WORD = re.compile(r"[^\W\d_]{3,}")
_YEAR_IN_TEXT = re.compile(r"\b(?:19|20)\d\d\b")
# A year is not compared either: EN names the month where ID says "bulan sebelumnya" ("in July
# 2026"), and a period that differs is a matter of meaning.
_YEAR = re.compile(r"(?:19|20)\d\d")
# Indonesian prints 10.448,1 and 9.388 (thousands); English prints 10,448.1. A number glued to a
# letter (M2, PS01) is a name, not a figure, in both; the currency prefix Rp is the exception.
_START = r"(?:(?<![\w.,])|(?<=Rp))"
_NUMBER = {
    "id": re.compile(_START + r"(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)(?![\w])"),
    "en": re.compile(_START + r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])"),
}
# English writes a quarter as Q2 and an ordinal as 2nd where Indonesian writes Triwulan II and
# kedua: neither number has a counterpart, so neither is compared.
_EN_NOT_NUMBERS = re.compile(r"\bQ[1-4]\b|\b\d+(?:st|nd|rd|th)\b")
# An Indonesian-format number left in the English text (UB Juni 2026 press release: "Rp10.432,8
# trillion"): read for its value, and flagged as a format slip.
_ID_FORMAT_IN_EN = re.compile(_START + r"\d{1,3}(?:\.\d{3})+,\d+(?![\w])")

Lang = Literal["id", "en"]


@dataclass
class Pair:
    index: int
    id_text: Optional[str]
    en_text: Optional[str]
    id_pos: Optional[int] = None   # the paragraph's position in its document's paragraphs()
    en_pos: Optional[int] = None


@dataclass
class Located:
    """A paragraph and where a reader finds it: the page of a PDF, the capitalised section heading
    above it (a Word document has no fixed pages)."""
    text: str
    page: Optional[int]
    section: Optional[str]


# A heading in title case: "Kapasitas Produksi Terpakai", "Lapangan Usaha Perdagangan Besar dan
# Eceran, dan Reparasi Mobil dan Motor", "Agricultural, Forestry, and Fishery Sector", a chart's
# own title, "Source: BIBS (BI); GDP (BPS)". Short, no sentence end, nearly every longer word
# capitalised. The two versions title their sections and charts differently (SKDU Tw III-2026: the
# Indonesian prints chart titles the English keeps inside the picture), and with no figure to go
# by these lines paired by position, slid out of step, and the model compared a heading with an
# unrelated sentence (2026-11-05: most of 43 "makna" findings). They are headings, like the ones
# in capitals.
_HEADING_MAX_WORDS = 16
_CAPITALISED = re.compile(r"^[(\[\"'“‘]*[A-Z]")


def _is_heading(line: str) -> bool:
    # A note under a table ("**Sektor Lainnya mencakup Pemda, Koperasi, …") is text, whatever its
    # capitals: the English note is written in lower case and pairs with it.
    if line.startswith("*") or re.search(r"[.!?]$", line) or len(line.split()) > _HEADING_MAX_WORDS:
        return False
    words = [w for w in line.split() if len(_WORD.findall(w)) and len(w.strip("(),;:")) > 3]
    if not words:
        return False
    return sum(1 for w in words if _CAPITALISED.match(w)) / len(words) >= 0.75


def located_paragraphs(narrative_text: str) -> List[Located]:
    """The narrative's paragraphs, without page markers, blank lines or trivial fragments, each
    with the page marker and the capitalised heading last seen above it."""
    out: List[Located] = []
    page: Optional[int] = None
    section: Optional[str] = None
    for line in narrative_text.splitlines():
        line = line.strip()
        marker = _PAGE_MARKER.match(line)
        if marker:
            page = int(marker.group(1))
            continue
        if not line:
            continue
        has_figure = re.search(r"\d", _YEAR_IN_TEXT.sub("", line)) is not None
        # A section heading in capitals is translated freely ("PERKEMBANGAN DANA PIHAK KETIGA
        # (DPK)" / "DEPOSITS"): comparing it would only flag wording. It still says where the
        # paragraphs under it are.
        if not has_figure and (line.isupper() or _is_heading(line)):
            section = line
            continue
        if len(_WORD.findall(line)) >= 3 or has_figure:
            out.append(Located(line, page, section))
    return out


def located_headings(narrative_text: str) -> List[Located]:
    """The narrative's headings in reading order (in capitals or in title case, see _is_heading):
    what located_paragraphs leaves out of the pairing, for the heading check (_heading_check)."""
    out: List[Located] = []
    page: Optional[int] = None
    for line in narrative_text.splitlines():
        line = line.strip()
        marker = _PAGE_MARKER.match(line)
        if marker:
            page = int(marker.group(1))
            continue
        if not line or re.search(r"\d", _YEAR_IN_TEXT.sub("", line)):
            continue
        # A source line names the data, not a part of the report: "Sumber: SKDU (BI)" against
        # "Source: BIBS (BI)" was read as a difference on the first SKDU run.
        if re.match(r"(?i)(sumber|source)\s*:", line):
            continue
        if line.isupper() or _is_heading(line):
            out.append(Located(line, page, line))
    return out


def paragraphs(narrative_text: str) -> List[str]:
    """The narrative's paragraphs, without page markers, blank lines or trivial fragments."""
    return [p.text for p in located_paragraphs(narrative_text)]


def _id_value(token: str) -> float:
    return float(token.replace(".", "").replace(",", "."))


def _tokens(text: str, lang: Lang) -> List[Tuple[float, str]]:
    """(unsigned value, token as printed) for every number in the text but years, in order."""
    found = []
    if lang == "en":
        text = _EN_NOT_NUMBERS.sub(lambda m: " " * len(m.group(0)), text)
        for m in _ID_FORMAT_IN_EN.finditer(text):
            found.append((m.start(), _id_value(m.group(0)), m.group(0)))
        text = _ID_FORMAT_IN_EN.sub(lambda m: " " * len(m.group(0)), text)
    for m in _NUMBER[lang].finditer(text):
        token = m.group(0)
        if _YEAR.fullmatch(token):
            continue
        value = float(token.replace(",", "")) if lang == "en" else _id_value(token)
        found.append((m.start(), value, token))
    return [(value, token) for _, value, token in sorted(found)]


def _id_format_in_en(text: str) -> List[str]:
    return [m.group(0) for m in _ID_FORMAT_IN_EN.finditer(_EN_NOT_NUMBERS.sub(" ", text))]


def numbers(text: str, lang: Lang) -> Counter:
    """The paragraph's numbers as unsigned values; the sign is a matter of meaning ("turun 0,5" /
    "-0,5" / "contracted by 0.5")."""
    return Counter(value for value, _ in _tokens(text, lang))


def _signature(text: str, lang: Lang) -> Tuple[float, ...]:
    return tuple(sorted(numbers(text, lang).elements()))


def align(id_paras: List[str], en_paras: List[str]) -> List[Pair]:
    """Pairs in reading order. The two are matched on each paragraph's numbers, so a paragraph
    missing on one side leaves the rest in step; inside a stretch that differs, paragraphs pair by
    position and the surplus stands alone."""
    matcher = difflib.SequenceMatcher(a=[_signature(p, "id") for p in id_paras],
                                      b=[_signature(p, "en") for p in en_paras], autojunk=False)
    pairs: List[Pair] = []
    for _, i1, i2, j1, j2 in matcher.get_opcodes():
        for k in range(max(i2 - i1, j2 - j1)):
            i, j = (i1 + k if i1 + k < i2 else None), (j1 + k if j1 + k < j2 else None)
            pairs.append(Pair(len(pairs), None if i is None else id_paras[i], None if j is None else en_paras[j], i, j))
    return pairs


def _only(tokens: List[Tuple[float, str]], other: Counter) -> List[str]:
    left = Counter(other)
    out = []
    for value, token in tokens:
        if left[value]:
            left[value] -= 1
        else:
            out.append(token)
    return out


def number_finding(pair: Pair) -> Optional[TranslationFinding]:
    """The numbers one side prints and the other does not, an Indonesian-format number in the
    English text, or a paragraph with no pair at all."""
    if pair.id_text is None or pair.en_text is None:
        side, text = ("ID", pair.id_text) if pair.en_text is None else ("EN", pair.en_text)
        tokens = [t for _, t in _tokens(text, "id" if side == "ID" else "en")]
        explanation = f"Paragraf ini hanya ada di versi {side}"
        if not tokens:
            return TranslationFinding(kind="makna", explanation=explanation)
        return TranslationFinding(kind="angka", explanation=explanation,
                                  only_id=tokens if side == "ID" else [], only_en=tokens if side == "EN" else [])
    id_tokens, en_tokens = _tokens(pair.id_text, "id"), _tokens(pair.en_text, "en")
    only_id = _only(id_tokens, Counter(v for v, _ in en_tokens))
    only_en = _only(en_tokens, Counter(v for v, _ in id_tokens))
    wrong_format = _id_format_in_en(pair.en_text)
    if not only_id and not only_en and not wrong_format:
        return None
    parts = []
    if only_id or only_en:
        sides = [f"hanya di ID: {', '.join(only_id)}" if only_id else "",
                 f"hanya di EN: {', '.join(only_en)}" if only_en else ""]
        parts.append("Angka berbeda — " + " · ".join(side for side in sides if side))
    if wrong_format:
        parts.append(f"Format angka Indonesia di versi EN: {', '.join(wrong_format)}")
    return TranslationFinding(kind="angka", explanation="; ".join(parts), only_id=only_id,
                              only_en=only_en + [t for t in wrong_format if t not in only_en])


# ── The meaning pass ─────────────────────────────────────────────────────────────────────────────

class MeaningFinding(BaseModel):
    pair: int = Field(description="The PAIR number the difference is in")
    id_quote: str = Field(description="The exact fragment of the ID text that differs")
    en_quote: str = Field(description="The exact fragment of the EN text that differs")
    explanation: str = Field(description="One sentence in Indonesian saying what differs, not which side is right")


class MeaningFindings(BaseModel):
    findings: List[MeaningFinding] = Field(default_factory=list)


SYSTEM_PROMPT = """You compare paragraphs of an Indonesian statistical publication (Bank Indonesia) \
with their English version. The English text is meant to be a direct translation of the Indonesian \
one. Each PAIR holds one Indonesian paragraph (ID) and its English counterpart (EN).

Report only real differences in content:
- For EVERY number, check that in both versions it refers to the same subject or component, the \
same period and the same direction. Numbers swapped between items ("masing-masing" / \
"respectively") are a difference.
- Direction words: naik/meningkat/tumbuh/akselerasi against fell/decreased/slowed, and the reverse.
- Periods: month, quarter, year, yoy against mtm, "bulan sebelumnya" against a named month that is \
not the previous one.
- Subject: the indicator, component, sector or region named. Usual Bank Indonesia equivalents: \
giro = demand deposits, tabungan = savings deposits, simpanan berjangka / deposito = time \
deposits, valas = foreign currency, uang kuasi = quasi-money, uang beredar sempit (M1) = narrow \
money, uang beredar luas (M2) = broad money, uang kartal = currency, DPK = third-party funds \
(TPF), kredit modal kerja / investasi / konsumsi = working capital / investment / consumer loans. \
Use them to recognise the same thing: another English term for the same component (third-party \
deposits for DPK) is not a difference; a different component (time deposits for giro) is.
- Units and scale: triliun = trillion, miliar = billion, juta = million, persen = percent.

Do not report wording, style, word order, added or dropped connecting words, or number formatting \
(10.448,1 is the same as 10,448.1). The same items listed in another order, each still with its own \
number, are not a difference. Do not judge which version is right.

For each difference give the PAIR number, the exact differing fragment of each text, and one \
sentence in Indonesian saying what differs. An empty list is the normal answer."""

_PROMPT = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", "{pairs}")])


# ── The heading check ────────────────────────────────────────────────────────────────────────────
# Headings stay out of the paragraph pairing: the two versions title their sections and charts
# differently, and with no number to go by they paired by position and slid out of step (SKDU Tw
# III-2026). They are paired by the model instead, on what they mean, in one call per document.

class HeadingFinding(BaseModel):
    id_no: int = Field(description="The number of the Indonesian heading")
    en_no: int = Field(description="The number of its English counterpart")
    explanation: str = Field(description="One sentence in Indonesian saying what differs, not which side is right")


class HeadingFindings(BaseModel):
    findings: List[HeadingFinding] = Field(default_factory=list)


HEADING_PROMPT = """You compare the headings of an Indonesian statistical publication (Bank Indonesia) with those of its English version: section titles, sector titles, chart and table titles. You get two numbered lists, each in reading order. The two versions do not title every part the same way: one may split a section into more headings, and chart titles may exist on one side only (the other prints them inside the picture).

First pair each Indonesian heading with the English heading that names the same part, where there is one. Keep the reading order. Many headings have no counterpart; leave those alone. When you are not sure two headings name the same part, they are not a pair.

Then report a pair ONLY when the two headings differ in substance:
- a different sector, industry, component, indicator, instrument or region;
- an item of a named list dropped, added or replaced ("Pengadaan Listrik dan Gas" against "Electricity Supply" drops gas);
- a different period or direction.

Do not report: a heading with no counterpart; wording or style; generic words added or dropped (Perkembangan, Kinerja, Lapangan Usaha, LU, Sektor, Sector, Development, Growth); the usual Bank Indonesia equivalents (lapangan usaha = sector or industry, pertanian, kehutanan, dan perikanan = agriculture, forestry and fishery, penggunaan tenaga kerja = labour utilisation, harga jual = selling prices, SKDU = BIBS, prakiraan = forecast, expectation, outlook or projection, realisasi = realised or actual); a heading that names a section more broadly or more narrowly than its counterpart ("Inflasi" against "Inflation Expectation"). An empty list is the normal answer."""

_HEADING_PROMPT = ChatPromptTemplate.from_messages([("system", HEADING_PROMPT), ("human", "{lists}")])


async def _heading_check(id_heads: List[Located], en_heads: List[Located], llm) -> List[HeadingFinding]:
    """The heading pairs that differ in substance, as the model pairs and reads them."""
    lists = ("INDONESIAN HEADINGS\n" + "\n".join(f"{i}. {h.text}" for i, h in enumerate(id_heads, 1))
             + "\n\nENGLISH HEADINGS\n" + "\n".join(f"{j}. {h.text}" for j, h in enumerate(en_heads, 1)))
    answer = await (_HEADING_PROMPT | llm.with_structured_output(HeadingFindings)).ainvoke({"lists": lists})
    return [f for f in answer.findings if 1 <= f.id_no <= len(id_heads) and 1 <= f.en_no <= len(en_heads)]


def _batches(pairs: List[Pair], max_chars: int) -> List[List[Pair]]:
    """Two-sided pairs, in order, grouped so each batch's text stays under max_chars."""
    batches: List[List[Pair]] = []
    size = 0
    for pair in pairs:
        if pair.id_text is None or pair.en_text is None:
            continue
        length = len(pair.id_text) + len(pair.en_text)
        if batches and size + length <= max_chars:
            batches[-1].append(pair)
            size += length
        else:
            batches.append([pair])
            size = length
    return batches


async def _meaning(batch: List[Pair], llm) -> List[MeaningFinding]:
    text = "\n\n".join(f"[PAIR {p.index}]\nID: {p.id_text}\nEN: {p.en_text}" for p in batch)
    answer = await (_PROMPT | llm.with_structured_output(MeaningFindings)).ainvoke({"pairs": text})
    return answer.findings


async def _no_headings() -> List[HeadingFinding]:
    return []


def _add_heading_findings(out: List[TranslationPair], answer, id_heads: List[Located], en_heads: List[Located],
                          id_is_word: bool, en_is_word: bool) -> None:
    """Each heading pair the model found different, after the paragraph pairs. A failed call
    leaves one unchecked entry, so the headings never read as checked and clean."""
    if isinstance(answer, BaseException):
        logger.warning("Translation heading check failed: %s", answer)
        out.append(TranslationPair(index=len(out), id_text="Judul-judul bagian dan grafik", checked=False))
        return
    for found in answer:
        id_head, en_head = id_heads[found.id_no - 1], en_heads[found.en_no - 1]
        out.append(TranslationPair(
            index=len(out), id_text=id_head.text, en_text=en_head.text,
            id_page=None if id_is_word else id_head.page, en_page=None if en_is_word else en_head.page,
            id_section="Judul", en_section="Judul",
            findings=[TranslationFinding(kind="makna", explanation=found.explanation,
                                         id_quote=id_head.text, en_quote=en_head.text)]))


def _where(located: List[Located], pos: Optional[int], is_word: bool) -> Tuple[Optional[int], Optional[str]]:
    """(page, section) for a paragraph: a PDF's page, a Word document's section heading. A Word
    narrative's [== Halaman N ==] blocks are not pages (UB Agu 2026: off by up to nine), and a
    PDF's page is enough to find a paragraph."""
    if pos is None:
        return None, None
    return (None, located[pos].section) if is_word else (located[pos].page, None)


async def check_translation(id_text: str, en_text: str, llm, *, document: str, id_filename: str,
                            en_filename: str, id_is_word: bool = False, en_is_word: bool = False,
                            max_chars: int = 6000, heading_llm=None) -> TranslationCheckResponse:
    """The English narrative against the Indonesian one: pairs, the numbers each side prints
    alone (code), and the differences in meaning the LLM finds per batch of pairs. A batch the LLM
    could not answer leaves its pairs unchecked, never clean. Each pair says where its paragraphs
    are: the page in a PDF, the section in a Word document (`*_is_word`)."""
    id_located, en_located = located_paragraphs(id_text), located_paragraphs(en_text)
    # Nothing read (a scanned PDF without a text layer) must not come back as "no differences".
    for paras, side in ((id_located, "versi Indonesia"), (en_located, "versi Inggris")):
        if not paras:
            raise ValueError(f"Tidak ada narasi yang terbaca dari dokumen {side}. Unggah versi Word (.docx) "
                             "atau PDF yang teksnya bisa disalin.")
    pairs = align([p.text for p in id_located], [p.text for p in en_located])
    out = []
    for p in pairs:
        id_page, id_section = _where(id_located, p.id_pos, id_is_word)
        en_page, en_section = _where(en_located, p.en_pos, en_is_word)
        out.append(TranslationPair(index=p.index, id_text=p.id_text, en_text=p.en_text, id_page=id_page,
                                   en_page=en_page, id_section=id_section, en_section=en_section))
    for pair, item in zip(pairs, out):
        finding = number_finding(pair)
        if finding is not None:
            item.findings.append(finding)
    batches = _batches(pairs, max_chars)
    id_heads, en_heads = located_headings(id_text), located_headings(en_text)
    # The headings go to `heading_llm` when given: on SKDU Tw III-2026 the plain model reported 4-5
    # equivalent titles as different ("Prakiraan Inflasi" / "Inflation Expectation") on every run,
    # the review model (thinking a little) 0-1, the one a real difference.
    heading_call = (_heading_check(id_heads, en_heads, heading_llm or llm) if id_heads and en_heads
                    else _no_headings())
    *answers, heading_answer = await asyncio.gather(*(_meaning(b, llm) for b in batches), heading_call,
                                                    return_exceptions=True)
    for batch, answer in zip(batches, answers):
        if isinstance(answer, BaseException):
            logger.warning("Translation meaning check failed for pairs %s: %s", [p.index for p in batch], answer)
            for pair in batch:
                out[pair.index].checked = False
            continue
        inside = {p.index for p in batch}
        for found in answer:
            if found.pair not in inside:
                logger.warning("Meaning finding for pair %s outside its batch dropped", found.pair)
                continue
            out[found.pair].findings.append(TranslationFinding(
                kind="makna", explanation=found.explanation, id_quote=found.id_quote, en_quote=found.en_quote))
    _add_heading_findings(out, heading_answer, id_heads, en_heads, id_is_word, en_is_word)
    findings = [f for item in out for f in item.findings]
    return TranslationCheckResponse(
        document=document, id_filename=id_filename, en_filename=en_filename, pairs=out,
        n_number=sum(f.kind == "angka" for f in findings), n_meaning=sum(f.kind == "makna" for f in findings),
        n_unchecked=sum(not item.checked for item in out))
