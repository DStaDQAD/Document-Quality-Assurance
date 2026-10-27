"""A whole strict check: the tables as views, the claims, their verdicts, the Tinjauan AI, the response.

verify_paired hands a check over here when STRICT_EXTRACTION is on and the check is Excel-only
(`applies`); everything before that point — reading the document, parsing the sheets, the team's
settings — is shared with the default path.
"""
import logging
import os
from typing import Callable, Dict, List, Optional

from langchain_core.language_models import BaseChatModel

from paired_verifier import _ExcelSource
from publication_parsers import notes_for, skipped_sections
from schemas import PairedVerificationResponse
from strict.claims import claim_to_dict
from strict.evaluate import evaluate_claim
from strict.extract import extract_claims
from strict.tables import build_views
from structured_extractor import drop_sections, extraction_llms

logger = logging.getLogger("fact-checker")


def enabled() -> bool:
    """STRICT_EXTRACTION=1/on/true/yes."""
    return os.getenv("STRICT_EXTRACTION", "").strip().lower() in ("1", "on", "true", "yes")


def applies(sources: List[_ExcelSource], mode: str, has_charts: bool) -> bool:
    """Strict reads the uploaded Excel time-series tables and, in "both" mode — the team's usual
    check, press release and report together — the document's own tables and the other
    document's after them. It needs at least one uploaded workbook. Chart readings and a category
    (non-time-series) sheet keep the old path, as do "internal" and "none"."""
    if mode not in ("excel", "both") or has_charts:
        return False
    if any(s.origin == "excel" and not s.pointer_only and s.table.axis_type == "categorical" for s in sources):
        return False
    # The document's own tables join the uploaded workbooks; they never stand in for them.
    return any(view.source.origin == "excel" for view in build_views(sources))


def _hints(team_aliases: Dict[str, str]) -> str:
    return "\n".join(f"\"{phrase}\" → '{row}'" for phrase, row in team_aliases.items())


async def run_check(
    narrative_text: str,
    sources: List[_ExcelSource],
    llm: BaseChatModel,
    *,
    pdf_filename: str,
    vision_llm: Optional[BaseChatModel],
    publication: str,
    team_aliases: Dict[str, str],
    team_notes: str,
    review_llm: Optional[BaseChatModel],
    review_new_refuted: str,
    progress_cb: Optional[Callable[[dict], None]],
    on_extract_gap: Optional[Callable],
    excel_parsers: List[str],
    mode: str = "excel",
) -> PairedVerificationResponse:
    def emit(stage: str, status: str, **extra) -> None:
        if progress_cb is not None:
            progress_cb({"type": "stage", "stage": stage, "status": status, **extra})

    views = build_views(sources)

    def _on_progress(done: int, total: int) -> None:
        emit("extract", "running", current=done, total=total, detail=f"{total} bagian teks")

    primary, fallback = extraction_llms(llm, vision_llm)
    claims = await extract_claims(
        drop_sections(narrative_text, skipped_sections(publication)), views, primary,
        fallback_llm=fallback,
        notes=notes_for(publication), hints=_hints(team_aliases),
        on_progress=_on_progress, on_chunk_failed=on_extract_gap,
    )
    emit("extract", "done", detail=f"{len(claims)} klaim ditemukan")
    emit("compare", "running", detail=f"{len(claims)} klaim")
    results = [evaluate_claim(claim, views) for claim in claims]
    n_reviewed = 0
    if review_llm is not None and results:
        # Imported here as verify_paired does: ai_review builds on paired_verifier.
        from ai_review import review_claims
        try:
            results, stats = await review_claims(results, [v.source for v in views], review_llm,
                                                 new_refuted=review_new_refuted, team_notes=team_notes)
            n_reviewed = stats.changed
        except Exception:
            logger.warning("Tinjauan AI gagal; vonis kode dipertahankan", exc_info=True)
    entailed = sum(r.verdict == "Entailed" for r in results)
    refuted = sum(r.verdict == "Refuted" for r in results)
    inconclusive = sum(r.verdict == "Inconclusive" for r in results)
    detail = f"{entailed} sesuai · {refuted} tidak sesuai · {inconclusive} tidak dapat dipastikan"
    emit("compare", "done", detail=detail + (f" · {n_reviewed} diubah tinjauan AI" if n_reviewed else ""))
    return PairedVerificationResponse(
        pdf_filename=pdf_filename,
        excel_filenames=[s.filename for s in sources],
        excel_sheets=[s.sheet for s in sources],
        excel_units=[s.table.unit for s in sources],
        excel_parsers=excel_parsers,
        mode=mode,
        total_facts=len(results),
        entailed_count=entailed,
        refuted_count=refuted,
        inconclusive_count=inconclusive,
        results=results,
        strict_claims=[claim_to_dict(c) for c in claims],
    )
