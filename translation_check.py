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
        if line.isupper() and not has_figure:
            section = line
            continue
        if len(_WORD.findall(line)) >= 3 or has_figure:
            out.append(Located(line, page, section))
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


def _where(located: List[Located], pos: Optional[int], is_word: bool) -> Tuple[Optional[int], Optional[str]]:
    """(page, section) for a paragraph: a PDF's page, a Word document's section heading. A Word
    narrative's [== Halaman N ==] blocks are not pages (UB Agu 2026: off by up to nine), and a
    PDF's page is enough to find a paragraph."""
    if pos is None:
        return None, None
    return (None, located[pos].section) if is_word else (located[pos].page, None)


async def check_translation(id_text: str, en_text: str, llm, *, document: str, id_filename: str,
                            en_filename: str, id_is_word: bool = False, en_is_word: bool = False,
                            max_chars: int = 6000) -> TranslationCheckResponse:
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
    answers = await asyncio.gather(*(_meaning(b, llm) for b in batches), return_exceptions=True)
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
    findings = [f for item in out for f in item.findings]
    return TranslationCheckResponse(
        document=document, id_filename=id_filename, en_filename=en_filename, pairs=out,
        n_number=sum(f.kind == "angka" for f in findings), n_meaning=sum(f.kind == "makna" for f in findings),
        n_unchecked=sum(not item.checked for item in out))
