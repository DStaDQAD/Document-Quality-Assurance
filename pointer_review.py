"""Tinjauan AI for table pointers: which table a sentence's "(Tabel N)" should name, read from the
sentence's subject rather than its numbers.

document_checks settles a pointer by the numbers: when the cited table prints the sentence's
figures the pointer holds, when one other table prints them it does not. It leaves the rest open:
a sentence with no figure, figures that are wrong, derived ("4,5 miliar dari ketiga kawasan"),
printed in several tables or in a picture the reader cannot read (PNG, a PDF). Those go to a model
with the report's table titles, and the row labels of each table whose picture was read, and the
model names the table the sentence is about.

A pointer whose figures the cited table prints is never sent, so the model cannot overrule it.
A model answer only adds a Tidak Sesuai card when it says the cited table is about something
else and names one listed table that is; when unsure it is told to let the pointer stand.
"""
import logging
import re
from typing import Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from document_checks import Pointer, open_pointers
from schemas import FactVerificationResult

logger = logging.getLogger("fact-checker")

RESOLVED_VIA = "document_check_ai"

_SYSTEM = """You check a Bank Indonesia report's pointers to its own tables. Each sentence below \
names the table where the reader finds what it reports, e.g. "(Tabel 3)" or "(Lampiran 4 dan 5)". \
You get the report's tables: their number, title and, for some, their row labels.

For each sentence decide whether a cited table shows what the sentence reports: the indicator \
and how it is broken down (by country, by commodity group, by sector, by currency, by customer \
group, ...). Judge by subject only. The sentence's numbers may be wrong and must not decide. A \
sentence often takes its subject from the text before it ("Hal tersebut tecermin dari SBT ..."), \
given as "sebelumnya": read the sentence in that context.

- fits = true when a cited table shows it, also when another table shows it as well, and \
whenever you are unsure.
- fits = false only when every cited table is clearly about something else (another indicator, \
or another breakdown of it) and one listed table clearly shows what the sentence reports.
- table: the listed table that best shows what the sentence reports, written exactly as listed \
("Tabel 5", "Lampiran 4").
- about: what the sentence reports, in a few Indonesian words.

Answer every sentence exactly once, by its index."""

_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SYSTEM),
    ("human", "TABEL LAPORAN\n{tables}\n\nKALIMAT\n{sentences}"),
])


class _Judgement(BaseModel):
    index: int = Field(description="the sentence's index in the list")
    fits: bool = Field(description="true when a cited table shows what the sentence reports")
    table: str = Field(description="the listed table that best shows it, e.g. 'Tabel 5'")
    about: str = Field(description="what the sentence reports, in a few Indonesian words")


class _Judgements(BaseModel):
    items: List[_Judgement]


# Row labels of a read picture, its numbers dropped: enough to tell "menurut Negara Asal" from
# "menurut Kelompok Barang" when the titles are close.
_NUMBER_RE = re.compile(r"[-−]?\d[\d.,]*%?\*?")
_MAX_LABEL_WORDS = 40


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


def _card(pointer: Pointer, judged: _Judgement, titles: Dict[str, str]) -> FactVerificationResult:
    cited = " dan ".join(pointer.cited)
    cited_titles = "; ".join(f"'{titles[key]}'" for key in pointer.cited if titles.get(key))
    return FactVerificationResult(
        operation="reference", metric_label=f"Rujukan {cited}", verdict="Refuted",
        context_quote=pointer.sentence, page_number=pointer.page, resolved_via=RESOLVED_VIA,
        reasoning=(f"Kalimat ini tentang {judged.about.strip() or 'hal lain'}, yang ditampilkan di "
                   f"{judged.table} ('{titles[judged.table]}'), bukan di {cited}"
                   + (f" ({cited_titles})" if cited_titles else "")
                   + " | Dinilai Tinjauan AI dari isi kalimat dan judul tabel; angka kalimat tidak "
                     "tercetak di tabel yang dirujuk"),
    )


async def review_pointers(narrative_text: str, results: List[FactVerificationResult],
                          table_texts: Optional[Dict[str, str]], llm: BaseChatModel
                          ) -> List[FactVerificationResult]:
    """`results` with a Tidak Sesuai card for each open pointer the model says names the wrong
    table. One call per report; a report with fewer than two tables or no open pointer makes none."""
    titles, pointers = open_pointers(narrative_text, table_texts)
    if len(titles) < 2 or not pointers:
        return results
    sentences = "\n".join(f"{i}. [{' dan '.join(p.cited)}] {p.sentence}"
                          + (f"\n   sebelumnya: {p.before}" if p.before else "")
                          for i, p in enumerate(pointers))
    answer = await (_PROMPT | llm.with_structured_output(_Judgements)).ainvoke(
        {"tables": _tables_block(titles, table_texts), "sentences": sentences})
    out = list(results)
    seen = set()
    for judged in answer.items:
        if not 0 <= judged.index < len(pointers) or judged.index in seen:
            continue
        seen.add(judged.index)
        pointer = pointers[judged.index]
        table = judged.table.strip()
        if judged.fits or table not in titles or table in pointer.cited:
            continue
        judged.table = table
        out.append(_card(pointer, judged, titles))
    logger.info("Tinjauan AI rujukan tabel: %d kalimat terbuka, %d ditandai",
                len(pointers), len(out) - len(results))
    return out
