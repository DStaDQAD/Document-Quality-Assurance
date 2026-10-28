"""Cek Terjemahan: an English publication checked against its Indonesian version.

The English text is a direct translation, paragraph by paragraph, so the two are paired in order
and every number of a paragraph must appear in its pair. That part is code: the numbers are read
in each language's format and compared as sets, whatever order the sentence puts them in. What a
number is attached to, and the meaning around it (direction, period, subject, unit), is left to
the LLM. Differences are flagged; which version is wrong is the reviewer's call.
"""
import difflib
import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple

from schemas import TranslationFinding

_PAGE_MARKER = re.compile(r"^\[== Halaman \d+ ==\]$")
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


def paragraphs(narrative_text: str) -> List[str]:
    """The narrative's paragraphs, without page markers, blank lines or trivial fragments."""
    out = []
    for line in narrative_text.splitlines():
        line = line.strip()
        if not line or _PAGE_MARKER.match(line):
            continue
        has_figure = re.search(r"\d", _YEAR_IN_TEXT.sub("", line)) is not None
        # A section heading in capitals is translated freely ("PERKEMBANGAN DANA PIHAK KETIGA
        # (DPK)" / "DEPOSITS"): comparing it would only flag wording.
        if line.isupper() and not has_figure:
            continue
        if len(_WORD.findall(line)) >= 3 or has_figure:
            out.append(line)
    return out


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
            pairs.append(Pair(len(pairs), id_paras[i1 + k] if i1 + k < i2 else None,
                              en_paras[j1 + k] if j1 + k < j2 else None))
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
