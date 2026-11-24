"""A whole strict check: the tables as views, the claims, their verdicts, the Tinjauan AI, the response.

Every check goes through here (verify_paired hands it over after reading the document, parsing the
sheets and applying the team's settings): Word or PDF, time-series or category tables, whatever
parser read them. There is no other path for a check the app runs.
"""
import asyncio
import logging
from typing import Callable, Dict, List, Optional

from langchain_core.language_models import BaseChatModel

from document_checks import check_document
from pointer_review import review_pointers
from paired_verifier import _ExcelSource
from publication_parsers import skipped_sections
from schemas import PairedVerificationResponse
from strict.claims import Claim, claim_to_dict
from strict.evaluate import evaluate_claim
from strict.extract import extract_claims
from strict.tables import TableView, build_views, unreadable_sheets
from structured_extractor import drop_sections, extraction_llms

logger = logging.getLogger("fact-checker")


async def _from_documents(claims: List[Claim], text: str, documents: List[TableView], llm: BaseChatModel,
                          **extract_kw) -> List[Claim]:
    """Stage 2 ('both' mode): the sentences the workbooks could not answer, read again against the
    document's own tables and the other document's. A figure a workbook answers never reaches
    them, so a typo copied from a document's table into its narrative still meets the workbook."""
    sentences = list(dict.fromkeys(c.context_quote for c in claims if c.not_in_tables))
    if not sentences:
        return claims
    try:
        found = await extract_claims(text, documents, llm, focus=sentences, **extract_kw)
    except Exception:
        logger.warning("Strict: tabel dokumen tidak terbaca, klaim tetap tidak ada di workbook", exc_info=True)
        return claims
    answered = {(c.context_quote, c.claimed_value) for c in claims if not c.not_in_tables}
    by_sentence: Dict[str, List[Claim]] = {}
    for c in found:
        if not c.not_in_tables and c.context_quote in sentences and (c.context_quote, c.claimed_value) not in answered:
            by_sentence.setdefault(c.context_quote, []).append(c)
    # An absent claim is answered by a stage-2 claim on its number, or — when it carries no
    # number — by any stage-2 claim on its sentence.
    covered = {(c.context_quote, c.claimed_value) for group in by_sentence.values() for c in group}
    covered |= {(quote, None) for quote in by_sentence}
    merged: List[Claim] = []
    for c in claims:
        if c.not_in_tables:
            merged.extend(by_sentence.pop(c.context_quote, []))
            if (c.context_quote, c.claimed_value) in covered:
                continue
        merged.append(c)
    return merged


async def run_check(
    narrative_text: str,
    sources: List[_ExcelSource],
    llm: BaseChatModel,
    *,
    pdf_filename: str,
    vision_llm: Optional[BaseChatModel],
    publication: str,
    team_notes: str,
    review_llm: Optional[BaseChatModel],
    review_new_refuted: str,
    progress_cb: Optional[Callable[[dict], None]],
    on_extract_gap: Optional[Callable],
    excel_parsers: List[str],
    mode: str = "excel",
    table_texts: Optional[Dict[str, str]] = None,
    unreadable_tables: Optional[Dict[str, str]] = None,
    charts: Optional[list] = None,
    scanned: Optional[list] = None,
) -> PairedVerificationResponse:
    def emit(stage: str, status: str, **extra) -> None:
        if progress_cb is not None:
            progress_cb({"type": "stage", "stage": stage, "status": status, **extra})

    views = build_views(sources)
    # The charts are checked alongside the narrative (each chart is two model calls; the client
    # caps how many run at once), and waited for at the end.
    chart_task = None
    if charts or scanned:
        import chart_check  # here, as ai_review: it builds on strict
        emit("charts", "running")

        def _on_chart(done: int, total: int) -> None:
            emit("charts", "running", current=done, total=total, detail=f"{total} grafik")

        chart_task = asyncio.create_task(
            chart_check.check_charts(charts or [], scanned or [], views, llm, on_progress=_on_chart))
    try:
        workbooks = [v for v in views if v.source.origin == "excel"]
        documents = [v for v in views if v.source.origin != "excel"]

        def _on_progress(done: int, total: int) -> None:
            emit("extract", "running", current=done, total=total, detail=f"{total} bagian teks")

        primary, fallback = extraction_llms(llm, vision_llm)
        text = drop_sections(narrative_text, skipped_sections(publication))
        # The notes are Pengaturan's block (team_settings.for_check): the built-ins plus the team's
        # phrases and conventions, the same block the Tinjauan AI reads.
        extract_kw = dict(fallback_llm=fallback, notes=team_notes,
                          on_progress=_on_progress, on_chunk_failed=on_extract_gap)
        # Stage 1 shows the model the uploaded workbooks only: Excel first is the code's rule, not a
        # request the model may ignore.
        claims = await extract_claims(text, workbooks, primary, **extract_kw)
        if documents:
            claims = await _from_documents(claims, text, documents, primary, **extract_kw)
        emit("extract", "done", detail=f"{len(claims)} klaim ditemukan")
        emit("compare", "running", detail=f"{len(claims)} klaim")
        results = [evaluate_claim(claim, views) for claim in claims]
        n_reviewed = 0
        ai_notes: List[str] = []
        unread = unreadable_sheets(sources)
        if unread:
            ai_notes.append("Sheet ini tidak terbaca oleh parser mana pun (termasuk parser AI), sehingga klaim yang "
                            "membutuhkannya berputusan Tidak Cukup Data: " + "; ".join(unread) + ".")
        if review_llm is not None and results:
            # Imported here as verify_paired does: ai_review builds on paired_verifier.
            from ai_review import review_claims
            try:
                results, stats = await review_claims(results, [v.source for v in views], review_llm,
                                                     new_refuted=review_new_refuted, team_notes=team_notes)
                n_reviewed = stats.changed
                if stats.unanswered:
                    ai_notes.append(f"Tinjauan AI tidak menjawab {stats.unanswered} klaim; "
                                    "klaim itu memakai vonis dari pencocokan kode.")
            except Exception:
                logger.warning("Tinjauan AI gagal; vonis kode dipertahankan", exc_info=True)
                ai_notes.append("Tinjauan AI tidak selesai; semua klaim memakai vonis dari pencocokan kode.")
        # After the review, which never sees them: the report's own comparisons, table pointers and
        # table numbering, checked by the code alone.
        results = check_document(text, results, table_texts)
        # The table pointers the numbers leave open, and every chart pointer, read by the model from
        # each sentence's subject and the titles.
        try:
            results = await review_pointers(text, results, table_texts, llm, unreadable_tables)
        except Exception:
            logger.warning("Tinjauan AI rujukan tabel gagal; kartu dari kode tetap dipakai", exc_info=True)
            ai_notes.append("Rujukan tabel/grafik yang tidak bisa dipastikan dari angka tidak ditinjau AI; "
                            "hanya hasil pemeriksaan kode yang ditampilkan.")
        entailed =sum(r.verdict == "Entailed" for r in results)
        refuted = sum(r.verdict == "Refuted" for r in results)
        inconclusive = sum(r.verdict == "Inconclusive" for r in results)
        detail = f"{entailed} sesuai · {refuted} tidak sesuai · {inconclusive} tidak dapat dipastikan"
        emit("compare", "done", detail=detail + (f" · {n_reviewed} diubah tinjauan AI" if n_reviewed else ""))
    except BaseException:
        # Batalkan, or a failure in extraction or review: the chart task must not go on calling the
        # model (credits) for a check nobody will see.
        if chart_task is not None and not chart_task.done():
            chart_task.cancel()
        raise
    chart_checks = []
    if chart_task is not None:
        try:
            chart_checks, unread = await chart_task
            if unread:
                ai_notes.append("Grafik ini tidak terbaca sehingga tidak diperiksa: " + ", ".join(unread) + ".")
        except Exception:
            logger.warning("Cek grafik gagal; hasil narasi tetap dipakai", exc_info=True)
            ai_notes.append("Cek grafik tidak selesai; grafik tidak diperiksa.")
        n_labels = sum(c.label_count for c in chart_checks)
        emit("charts", "done", detail=f"{len(chart_checks)} grafik · {n_labels} label angka")
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
        ai_notes=ai_notes,
        chart_checks=chart_checks,
    )
