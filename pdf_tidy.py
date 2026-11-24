"""A PDF's text layer tidied by the model, page by page, under a lock that lets it move only spaces.

PDFium makes up spaces where a justified line spreads its letters: UB Agustus 2026 came out as
"Rp10. 448 ,1", "Ju li", "Pertumb uhan"; SK Agustus 2026 as "202 6" and "12 3,8". The pattern
depends on how each PDF was produced (UB April 2026, from the same team, is clean), so no rule
written for the patterns seen so far covers the next PDF. The model tidies the whole page instead,
and the program keeps its answer only when every paragraph is in the page letter for letter once
spaces and line breaks are set aside: it can join "12 3,8" into "123,8" and never make it "12,38".
A page whose answer is refused, leaves out prose, fails, or times out stays as it was.

Measured 2026-11-24 (Copilot Studio) against the Word text of the same reports: sentences with
numbers that come through intact, UB Agustus 22 → 68 of 73, SK Agustus 11 → 36 of 36, UB April
73 → 72 of 77 (a heading); split numbers 20 → 0, split words 30 → 0; no paragraph refused.
"""
import asyncio
import logging
import re
from typing import List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from pdf_extraction import PAGE_MARKER_RE

logger = logging.getLogger("fact-checker")

MAX_AT_ONCE = 4                   # pages with the model at once
MIN_PAGE_CHARS = 200              # a page with less text (a chart page, a cover) is not sent
MIN_COVER = 0.9                   # share of the page's prose lines the answer must keep

TIDY_PROMPT = """Berikut teks satu halaman laporan Bank Indonesia yang diambil dari lapisan teks PDF. Teks
ini bisa rusak: kata atau angka terbelah oleh spasi ("Ju li", "Rp10. 448 ,1", "202 6"), spasi
sebelum tanda baca ("(yoy) ,"), baris yang terpotong di tengah kalimat, dan header, footer, atau
nomor halaman yang menyela.

Tulis ulang teks halaman ini dengan rapi:
- sambungkan kata dan angka yang terbelah, rapikan spasi di sekitar tanda baca;
- satu paragraf per baris kosong; sambungkan baris yang terpotong di tengah kalimat;
- buang hanya nomor halaman dan header atau footer yang berulang di setiap halaman (nama laporan,
  logo); teks di dalam grafik atau tabel (label sumbu, angka tabel) boleh dibuang;
- JANGAN membuang judul bagian, judul grafik atau tabel, catatan kaki, sumber, atau keterangan:
  tulis semuanya, masing-masing sebagai paragraf sendiri.
JANGAN mengubah, menambah, atau menghapus huruf dan angka di dalam kalimat: hanya spasi dan
baris baru yang boleh berubah. Jangan menerjemahkan, meringkas, atau memberi komentar. Jawab
hanya dengan teks yang sudah rapi.

TEKS HALAMAN:
{page}"""

# The line-end hyphen PDFs store as U+FFFE or as a soft hyphen reads as "-". A minus sign is never
# folded away, so the model can neither drop nor add one.
_HYPHEN_LIKE = {"￾": "-", "­": "-", "‐": "-", "‑": "-"}
_WORD = re.compile(r"[A-Za-z]{3,}")


def _letters(text: str) -> str:
    """The text with every space and line break removed and line-end hyphens read as "-"."""
    return "".join(_HYPHEN_LIKE.get(ch, ch) for ch in re.sub(r"\s+", "", text))


def lock(page: str, answer: str) -> Optional[str]:
    """The model's tidy page, one paragraph per line, when every paragraph is in `page` letter for
    letter (spaces aside) and the paragraphs keep at least MIN_COVER of the page's prose lines;
    None otherwise."""
    original = _letters(page)
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", answer.strip())]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs or any(_letters(p) not in original for p in paragraphs):
        return None
    kept = _letters(" ".join(paragraphs))
    # Prose lines: long, with words (chart labels and table rows are short or mostly numbers).
    prose = [_letters(line) for line in page.splitlines()
             if len(line.strip()) >= 60 and len(_WORD.findall(line)) >= 8]
    if prose and sum(line[:25] in kept for line in prose) / len(prose) < MIN_COVER:
        return None
    return "\n".join(paragraphs)


def _content(message) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return content or ""


async def _tidy_page(page: str, llm: BaseChatModel, gate: asyncio.Semaphore, number: str) -> str:
    if len(page.strip()) < MIN_PAGE_CHARS:
        return page
    try:
        async with gate:
            answer = _content(await llm.ainvoke([HumanMessage(content=TIDY_PROMPT.format(page=page.strip()))]))
    except Exception as e:                          # the page as it was: the check goes on
        logger.warning("Halaman %s: teks PDF tidak dirapikan (%s)", number, e)
        return page
    tidy = lock(page, answer)
    if tidy is None:
        logger.info("Halaman %s: rapian AI ditolak kunci, teks asli dipakai", number)
        return page
    return tidy


async def tidy_text(text: str, llm: BaseChatModel) -> str:
    """`text` as extract_text_from_pdf gives it ("[== Halaman N ==]" + page, pages apart by a
    blank line), each page tidied by the model under the lock."""
    parts = PAGE_MARKER_RE.split(text)               # [before, number, body, number, body, …]
    numbers, bodies = parts[1::2], parts[2::2]
    gate = asyncio.Semaphore(MAX_AT_ONCE)
    tidied = await asyncio.gather(*(_tidy_page(body.strip("\n"), llm, gate, number)
                                    for number, body in zip(numbers, bodies)))
    return parts[0] + "\n\n".join(f"[== Halaman {n} ==]\n{body}" for n, body in zip(numbers, tidied))
