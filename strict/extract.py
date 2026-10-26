"""Strict extraction: one LLM call per chunk of the narrative, each answer finished into Claims.

The text handling — which lines are narrative, how pages are chunked, how a short anchor is found
again and grown to its sentence — is structured_extractor's, unchanged. What is new is what the
model is shown (the tables' shape, see tables.table_block) and what it answers (cells).
"""
import asyncio
import logging
from typing import Callable, Dict, List, Optional

from langchain_core.language_models import BaseChatModel

from strict.claims import Cell, Claim, StrictClaim, StrictClaims
from strict.prompt import PROMPT
from strict.tables import TableView, find_row, parse_period, table_block
from structured_extractor import (
    EXTRACTION_CHUNK_CHARS,
    _PAGE_MARKER_PATTERN,
    _expand_anchor_to_sentence,
    _filter_narrative,
    _locate_anchor,
    _locate_by_value,
    _normalize_ws,
    _page_at,
    _parse_indonesian_number,
    _raise_if_every_chunk_failed,
    _sig_words,
    _split_into_page_chunks,
)

logger = logging.getLogger("fact-checker")

# Claims whose sentence states no number to compare.
_NO_NUMBER = {"is_increasing", "is_decreasing", "is_stable"}


def _notes_block(notes: str, hints: str) -> str:
    parts = []
    if notes.strip():
        parts.append(notes.strip())
    if hints.strip():
        parts.append("PADANAN TIM (petunjuk nama, bukan aturan):\n" + hints.strip())
    return "\n\n".join(parts) + "\n\n" if parts else ""


async def _ask(llm, payload: dict, fallback_llm: Optional[BaseChatModel]) -> StrictClaims:
    try:
        return await (PROMPT | llm.with_structured_output(StrictClaims)).ainvoke(payload)
    except Exception as primary:
        if fallback_llm is None:
            raise
        logger.warning("Strict extraction failed (%s), retrying with the fallback model", primary)
        return await (PROMPT | fallback_llm.with_structured_output(StrictClaims)).ainvoke(payload)


def _cells(raw: StrictClaim, views: Dict[str, TableView]) -> List[Cell]:
    cells = []
    for ref in raw.cells:
        view = views.get((ref.table or "").strip().upper())
        cells.append(Cell(code=ref.table, row_text=ref.row, period_text=ref.period,
                          row=find_row(view, ref.row) if view else None, key=parse_period(ref.period)))
    return cells


def _finish(raw: StrictClaim, normalized: str, views: Dict[str, TableView]) -> Optional[Claim]:
    """The model's answer as a Claim, or None when it cannot be checked honestly: a number the
    claim needs is missing or unreadable, or neither the anchor nor the number is in the text."""
    value_text = (raw.claimed_value_raw or "").strip() or None
    value = _parse_indonesian_number(value_text) if value_text else None
    needs_number = not (raw.not_in_tables or raw.operation in _NO_NUMBER
                        or (raw.operation == "formula" and value_text is None))
    if needs_number and value is None:
        logger.warning("Strict claim without a readable number (%r), skipped: %r", value_text, raw.anchor_quote)
        return None
    words = _sig_words(raw.subject or "")
    span = _locate_anchor(raw.anchor_quote, normalized, metric_words=words, value_text=value_text)
    if span is None and value_text:
        span = _locate_by_value(value_text, normalized, words)
    if span is None and value_text and value_text.lstrip("-") not in normalized:
        logger.warning("Strict claim whose number is not in the text, skipped: %r", raw.anchor_quote)
        return None
    if span is not None:
        quote = _expand_anchor_to_sentence(normalized, span[0], span[1]) or raw.anchor_quote
        page = _page_at(normalized, span[0])
    else:
        quote, page = raw.anchor_quote, None
    return Claim(
        operation=raw.operation, subject=raw.subject, cells=[] if raw.not_in_tables else _cells(raw, views),
        context_quote=quote, page_number=page, claimed_value=value, unit=raw.unit, formula=raw.formula,
        formula_unit=raw.formula_unit, all_members=raw.all_members, size_of_negative=raw.size_of_negative,
        not_in_tables=raw.not_in_tables,
        reason=raw.reason,
    )


async def extract_claims(
    narrative_text: str,
    views: List[TableView],
    llm: BaseChatModel,
    *,
    fallback_llm: Optional[BaseChatModel] = None,
    notes: str = "",
    hints: str = "",
    max_chars_per_chunk: int = EXTRACTION_CHUNK_CHARS,
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_chunk_failed: Optional[Callable[[List[int], Exception], None]] = None,
) -> List[Claim]:
    """Every claim of the narrative, chunk by chunk in parallel, with its cells resolved against
    `views`. Raises structured_extractor.ExtractionFailedError when every chunk failed."""
    filtered = _filter_narrative(narrative_text)
    chunks = _split_into_page_chunks(filtered, max_chars_per_chunk)
    by_code = {v.code: v for v in views}
    notes_block = _notes_block(notes, hints)
    errors: List[Exception] = []
    done = 0
    if on_progress is not None:
        on_progress(0, len(chunks))

    async def _chunk(chunk: str) -> List[StrictClaim]:
        nonlocal done
        payload = {"notes_block": notes_block, "tables_block": table_block(views, chunk),
                   "narrative_text": chunk}
        try:
            answer = (await _ask(llm, payload, fallback_llm)).claims
        except Exception as exc:
            errors.append(exc)
            answer = []
            if on_chunk_failed is not None:
                on_chunk_failed([int(n) for n in _PAGE_MARKER_PATTERN.findall(chunk)], exc)
        done += 1
        if on_progress is not None:
            on_progress(done, len(chunks))
        return answer

    answers = await asyncio.gather(*[_chunk(chunk) for chunk in chunks])
    _raise_if_every_chunk_failed(errors, len(chunks))
    normalized = _normalize_ws(filtered)
    claims = [_finish(raw, normalized, by_code) for answer in answers for raw in answer]
    return [c for c in claims if c is not None]
