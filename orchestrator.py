"""Document-level pipeline: extract claims, verify each one, assemble a per-claim report."""

from claim_extraction import extract_claims
from db import get_readonly_db
from llm_provider import get_llm
from schemas import VerifyDocumentResponse
from verifier import build_judge_chain, build_sql_chain, verify_claims


def verify_document(document: str) -> VerifyDocumentResponse:
    extraction_llm = get_llm(temperature=0.0)
    claims = extract_claims(document, extraction_llm)

    if not claims:
        return VerifyDocumentResponse(
            total_claims=0,
            entailed_count=0,
            refuted_count=0,
            inconclusive_count=0,
            error_count=0,
            summary="No checkable factual claims about data in the available tables were found in the document.",
            results=[],
        )

    db = get_readonly_db()
    sql_chain = build_sql_chain(get_llm(temperature=0.0))
    judge_chain = build_judge_chain(get_llm(temperature=0.0))

    results = verify_claims(claims, db, sql_chain, judge_chain)

    entailed_count = sum(1 for r in results if r.status == "Entailed")
    refuted_count = sum(1 for r in results if r.status == "Refuted")
    inconclusive_count = sum(1 for r in results if r.status == "Inconclusive")
    error_count = sum(1 for r in results if r.status == "Error")

    summary = (
        f"{len(results)} claim(s) extracted: {entailed_count} entailed, "
        f"{refuted_count} refuted, {inconclusive_count} inconclusive, "
        f"{error_count} could not be verified due to a pipeline error."
    )

    return VerifyDocumentResponse(
        total_claims=len(results),
        entailed_count=entailed_count,
        refuted_count=refuted_count,
        inconclusive_count=inconclusive_count,
        error_count=error_count,
        summary=summary,
        results=results,
    )
