"""A trend claim's direction, read by the code from the words the extractor copied out of the sentence.

The extractor picks is_increasing / is_decreasing / is_stable, and it picks by what it takes the
sentence to mean. SKDU Tw III-2026 (planted by the mentor): "SB akses kredit sebesar 1,84% …, meski
lebih tinggi dibandingkan SB 2,45% …" came back is_decreasing — the numbers' direction, not the
words' — and the wrong word passed as Sesuai. So it also copies the direction words
(StrictClaim.direction_words), and when they are in the sentence and say otherwise, they decide.
"""
import re
from typing import Optional

# Longest phrases first within each direction is not needed: a phrase of one direction never
# contains a phrase of another, and two directions found at once decide nothing (see direction_of).
_UP = ("lebih tinggi", "lebih besar", "meningkat", "peningkatan", "naik", "kenaikan", "menguat",
       "penguatan", "melonjak", "bertambah", "melebar")
_DOWN = ("lebih rendah", "lebih kecil", "menurun", "penurunan", "turun", "melambat", "perlambatan",
         "melemah", "pelemahan", "termoderasi", "berkurang", "terkoreksi", "menyempit")
_STABLE = ("stabil", "relatif sama", "tidak berubah")
_NEGATION = re.compile(r"\b(?:tidak|tak|belum|bukan)\b")


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"'“”‘’()\[\],.;:]", " ", (text or "").lower())).strip()


def _has(words: str, phrases) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", words) for p in phrases)


def direction_of(words: Optional[str]) -> Optional[str]:
    """The trend operation the words state, or None when they state none, both ways, or a negation."""
    folded = _fold(words)
    if not folded:
        return None
    if _has(folded, _STABLE):
        return "is_stable" if not (_has(folded, _UP) or _has(folded, _DOWN)) else None
    if _NEGATION.search(folded):
        return None
    up, down = _has(folded, _UP), _has(folded, _DOWN)
    if up == down:
        return None
    return "is_increasing" if up else "is_decreasing"


def in_sentence(words: Optional[str], sentence: str) -> bool:
    """True when the copied words are in the sentence (case, spacing and punctuation aside)."""
    folded = _fold(words)
    return bool(folded) and folded in _fold(sentence)
