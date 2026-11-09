"""Tinjauan AI for table and chart pointers: which table or chart a sentence's "(Tabel N)" or
"(Grafik N)" should name, read from the sentence's subject rather than its numbers.

document_checks settles a table pointer by the numbers: when the cited table prints the
sentence's figures the pointer holds, when one other table prints them it does not. It leaves the
rest open: a sentence with no figure, figures that are wrong, derived ("4,5 miliar dari ketiga
kawasan"), printed in several tables or in a picture the reader cannot read (PNG, JPEG). A chart is
never read at all, so every chart pointer is open. Those go to a model with the report's table and
chart titles, and the row labels of each table whose picture was read, and the model names the
table or chart the sentence is about.

A pointer whose figures the cited table prints, and no other table of the same kind prints as
well, is never sent, so the model cannot overrule it. A model answer only adds a Tidak Sesuai card
when it says the cited table or chart is about something else and names one listed table or chart
that is; when unsure it lets the pointer stand. A table pointer it finds right gets a Sesuai card.
"""
import logging
import re
from typing import Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from document_checks import Pointer, open_pointers
from schemas import FactVerificationResult

logger = logging.getLogger("fact-checker")

RESOLVED_VIA = "document_check_ai"

_SYSTEM = """You check a Bank Indonesia report's pointers to its own tables and charts. Each \
sentence below names the table or chart where the reader finds what it reports, e.g. "(Tabel 3)", \
"(Lampiran 4 dan 5)" or "(Grafik 2)". You get the report's tables and charts: their number, title \
and, for some tables, their row labels. A chart is known by its title only.

For each sentence decide whether a cited table or chart shows what the sentence reports: the \
indicator and how it is broken down (by country, by commodity group, by sector, by currency, by \
customer group, ...). Judge by subject only. The sentence's numbers may be wrong and must not \
decide. A sentence often takes its subject from the text before it ("Hal tersebut tecermin dari \
SBT ..."), given as "sebelumnya", or the text after it names it ("Berdasarkan komponennya, ..."), \
given as "sesudahnya": read the sentence in that context.

- A sentence marked "angka juga di: <table>" prints figures that the cited table and that other \
table both show, so the numbers cannot tell them apart. Decide which of the two the sentence is \
about: the table whose subject is the sentence's own indicator and its breakdown, not a table that \
carries the indicator only as the total or opening row of something else (a table of the factors \
affecting money supply starts with the M2 row, a table of money supply and its components is about \
M2). For such a sentence, table = the one of the cited and the "angka juga di" tables that is \
about the sentence's subject; when they are equally about it, or you are unsure, the cited one.

- fits = true when a cited table or chart shows it, also when another one shows it as well, and \
whenever you are unsure.
- A chart, known by its title only, usually draws more than its title names: a composite index \
with its sub-indices (Indeks Keyakinan Konsumen with IKE and IEK; IKE with IPSI), a total with \
its breakdown, the realization with the forecast of the same indicator. So a cited chart fits \
whenever its title names the sentence's indicator or the broader indicator it belongs to; it is \
wrong only when its title names another indicator (nonmigas against migas, kegiatan usaha \
against kapasitas utilisasi).
- fits = false only when every cited one is clearly about something else (another indicator, or \
another breakdown of it) and one listed table or chart of the same kind clearly shows what the \
sentence reports.
- table: the listed table or chart of the same kind as the cited ones (a chart for "Grafik", a \
table for "Tabel" or "Lampiran") that best shows what the sentence reports, written exactly as \
listed ("Tabel 5", "Lampiran 4", "Grafik 3").
- about: what the sentence reports, in a few Indonesian words.

Answer every sentence exactly once, by its index."""

_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SYSTEM),
    ("human", "TABEL DAN GRAFIK LAPORAN\n{tables}\n\nKALIMAT\n{sentences}"),
])


class _Judgement(BaseModel):
    index: int = Field(description="the sentence's index in the list")
    fits: bool = Field(description="true when a cited table or chart shows what the sentence reports")
    table: str = Field(description="the listed table or chart that best shows it, e.g. 'Tabel 5', 'Grafik 3'")
    about: str = Field(description="what the sentence reports, in a few Indonesian words")


class _Judgements(BaseModel):
    items: List[_Judgement]


# Row labels of a read picture, its numbers dropped: enough to tell "menurut Negara Asal" from
# "menurut Kelompok Barang" when the titles are close.
_NUMBER_RE = re.compile(r"[-−]?\d[\d.,]*%?\*?")
# SBank's "Prioritas Target Pemberian Kredit Baru" lists its sectors after 40 words of credit types.
_MAX_LABEL_WORDS = 80

# Words every chart title shares, which say nothing of what it draws.
_TITLE_FILLER = {"perkembangan", "pertumbuhan", "indeks", "kelompok", "menurut", "berdasarkan",
                 "terhadap", "tahunan", "triwulanan", "bulanan", "saat", "nilai", "tingkat", "posisi",
                 "realisasi", "prakiraan", "dan", "per", "yoy", "persen"}


def _shares_subject(sentence: str, titles: List[str]) -> bool:
    """The sentence names something a cited chart's title names ("pendapatan" in "Komposisi
    Penggunaan Pendapatan Rumah Tangga"): judged from its title alone the chart may well draw it,
    so the model's doubt is not enough for a card."""
    def words(text: str) -> set:
        return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _TITLE_FILLER}
    said = words(sentence)
    return any(words(title) & said for title in titles)


def _labels(table_text: str) -> str:
    words = _NUMBER_RE.sub(" ", table_text).split()
    kept = list(dict.fromkeys(w for w in words if len(w) > 1))
    return " ".join(kept[:_MAX_LABEL_WORDS])


def _tables_block(titles: Dict[str, str], table_texts: Optional[Dict[str, str]]) -> str:
    lines = []
    for key, title in titles.items():
        line = f"- {key}: {title or '(tanpa judul)'}"
        if table_texts and table_texts.get(key):
            line += f" | baris: {_labels(table_texts[key])}"
        lines.append(line)
    return "\n".join(lines)


def _is_chart(key: str) -> bool:
    return key.startswith("Grafik ")


def _fits_card(pointer: Pointer, keys: List[str], judged: _Judgement,
               titles: Dict[str, str]) -> FactVerificationResult:
    """A Sesuai card for a table pointer the model found right, so the reader sees it was checked."""
    cited = " dan ".join(f"{key} ('{titles[key]}')" if titles.get(key) else key for key in keys)
    about = judged.about.strip()
    return FactVerificationResult(
        operation="reference", metric_label=f"Rujukan {' dan '.join(keys)}", verdict="Entailed",
        context_quote=pointer.sentence, page_number=pointer.page, resolved_via=RESOLVED_VIA,
        reasoning=(f"Kalimat ini tentang {about or 'isi tabel yang dirujuk'}, yang ditampilkan di {cited}"
                   + (f"; angkanya juga ada di {', '.join(pointer.tied)}" if pointer.tied else "")
                   + " | Dinilai Tinjauan AI dari isi kalimat dan judul tabel"),
    )


def _card(pointer: Pointer, keys: List[str], judged: _Judgement, titles: Dict[str, str],
          unreadable: Dict[str, str]) -> FactVerificationResult:
    cited = " dan ".join(keys)
    cited_titles = "; ".join(f"'{titles[key]}'" for key in keys if titles.get(key))
    if _is_chart(keys[0]):
        basis = "judul grafik; isi grafik tidak dibaca"
    else:
        unread = [f"isi {key} tidak terbaca ({unreadable[key].split(' — ', 1)[-1].split(',')[0]})"
                  for key in keys if key in unreadable]
        if pointer.tied and judged.table in pointer.tied:
            printed_where = f"angka kalimat tercetak di {cited} dan di {judged.table}, jadi angka tidak menentukan"
        else:
            printed_where = "angka kalimat tidak tercetak di tabel yang dirujuk"
        basis = "judul tabel; " + ("; ".join(unread) if unread else printed_where)
    return FactVerificationResult(
        operation="reference", metric_label=f"Rujukan {cited}", verdict="Refuted",
        context_quote=pointer.sentence, page_number=pointer.page, resolved_via=RESOLVED_VIA,
        reasoning=(f"Kalimat ini tentang {judged.about.strip() or 'hal lain'}, yang ditampilkan di "
                   f"{judged.table} ('{titles[judged.table]}'), bukan di {cited}"
                   + (f" ({cited_titles})" if cited_titles else "")
                   + f" | Dinilai Tinjauan AI dari isi kalimat dan {basis}"),
    )


async def review_pointers(narrative_text: str, results: List[FactVerificationResult],
                          table_texts: Optional[Dict[str, str]], llm: BaseChatModel,
                          unreadable: Optional[Dict[str, str]] = None) -> List[FactVerificationResult]:
    """`results` with a Tidak Sesuai card for each open pointer the model says names the wrong
    table or chart. One call per report; a report with fewer than two tables and charts, or with
    no open pointer, makes none. `unreadable`: word_extraction.WordDocument.unreadable_tables, named
    on a card whose table the reader could not read."""
    titles, items = open_pointers(narrative_text, table_texts)
    if len(titles) < 2 or not items:
        return results
    sentences = "\n".join(f"{i}. [{' dan '.join(keys)}] {p.sentence}"
                          + (f"\n   angka juga di: {', '.join(p.tied)}" if p.tied and not _is_chart(keys[0]) else "")
                          + (f"\n   sebelumnya: {p.before}" if p.before else "")
                          + (f"\n   sesudahnya: {p.after}" if p.after else "")
                          for i, (p, keys) in enumerate(items))
    answer = await (_PROMPT | llm.with_structured_output(_Judgements)).ainvoke(
        {"tables": _tables_block(titles, table_texts), "sentences": sentences})
    out = list(results)
    seen = set()
    for judged in answer.items:
        if not 0 <= judged.index < len(items) or judged.index in seen:
            continue
        seen.add(judged.index)
        pointer, keys = items[judged.index]
        table = judged.table.strip()
        # A tie: the model's pick between the cited table and the one printing the same figures
        # decides, whatever it says of the fit (it reads "another one shows it as well" as a fit).
        tied_pick = (not _is_chart(keys[0]) and table in pointer.tied and table not in keys)
        if tied_pick:
            judged.table = table
            out.append(_card(pointer, keys, judged, titles, unreadable or {}))
            continue
        if judged.fits and not _is_chart(keys[0]):
            out.append(_fits_card(pointer, keys, judged, titles))
            continue
        if (judged.fits or table not in titles or table in keys
                or _is_chart(table) != _is_chart(keys[0])):
            continue
        # A chart is known by its title only: a sentence naming what that title names keeps it.
        if _is_chart(keys[0]) and _shares_subject(pointer.sentence, [titles[key] for key in keys]):
            continue
        judged.table = table
        out.append(_card(pointer, keys, judged, titles, unreadable or {}))
    logger.info("Tinjauan AI rujukan tabel/grafik: %d rujukan terbuka, %d ditandai",
                len(items), sum(r.verdict == "Refuted" for r in out[len(results):]))
    return out
