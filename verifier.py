"""Verifies claims against the database via batched Text-to-SQL + LLM-as-a-Judge.

Both the SQL-generation step and the judge step take the *entire* list of claims for a document
in one LLM call instead of one call per claim. The database schema (and the judge's instructions)
would otherwise be re-sent as input tokens once per claim; batching sends them once per document
regardless of how many claims it contains. A single retry batch call covers every claim whose SQL
query failed to execute, rather than retrying each one individually.

The schema text comes from `db.get_table_info()` at call time, so newly added tables are picked up
automatically without any code change here.
"""

import ast
import logging
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

from langchain_community.utilities import SQLDatabase
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from db import get_distinct_value_hints
from schemas import ClaimVerificationResult, VerifyClaimResponse

logger = logging.getLogger("fact-checker")

SQL_GENERATION_SYSTEM_PROMPT = """You are a precise Text-to-SQL generator for a read-only database.

You are given a database schema and a numbered list of factual claims. For EACH claim, write
exactly one read-only SQL SELECT query that retrieves the data needed to verify that specific claim.

Rules:
- Only use tables and columns that appear in the schema.
- If a claim needs data from more than one table, join them directly in that claim's query.
- When a text column's exact known values are listed below the schema, use one of those exact
  strings verbatim in WHERE clauses (picking the closest match to the claim's wording) instead of
  inventing a plausible-looking string that doesn't actually occur in the column.
- When the hints include a "value combinations that actually occur together" list for a table,
  treat each listed tuple as fixed - if a claim's wording matches one value from a tuple, take the
  other column values for that WHERE clause from the SAME tuple. Never combine values pulled from
  different tuples, even if each value individually appears somewhere in its own column's list.
- Include a WHERE condition only for columns the claim text actually maps to. If the claim gives no
  information about a particular column (e.g. it never names a source file or sheet), leave that
  column out of the WHERE clause entirely - do not fill it in with a guessed value "just in case",
  since a wrong guess on an unreferenced column silently excludes the correct row from the result.
- Never write INSERT/UPDATE/DELETE/DROP or any other write statement - only SELECT.
- Prefer retrieving the precise rows/values relevant to each claim over a broad, unfiltered query -
  UNLESS the claim is a comparison, ranking, trend, or superlative (e.g. "tertinggi"/"highest",
  "lebih tinggi dibanding"/"higher than", "naik dari X ke Y"/"rose from X to Y") across multiple
  time periods or categories. For those claims, the query MUST retrieve every row needed for that
  comparison (e.g. all quarters of the relevant year, or both periods being compared) - do not
  filter down to only the single period named in the claim, or the comparison becomes impossible
  to verify.
- When a query (e.g. a self-join comparing two rows) would select more than one column that share
  the same underlying column name, give each one a distinct alias (e.g. `ef1.value AS value_a,
  ef2.value AS value_b`) - two selected columns with the same name silently collapse into one and
  lose data when the result is returned. Also include the label/period/category column each value
  belongs to (e.g. `ef1.row_label`, `ef1.col_label`) in the SELECT list, not just the bare value(s),
  so every returned number can be matched back to what it represents.
- Return exactly one query per claim, tagging each with its original claim_index.
"""

SQL_GENERATION_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SQL_GENERATION_SYSTEM_PROMPT),
        (
            "human",
            "Database schema:\n{schema}\n\n"
            "Known exact values for some text columns (use these verbatim, see the rule above):\n"
            "{distinct_values}\n\n"
            "Claims:\n{claims_block}\n"
            "{error_context}\n"
            "Write one read-only SQL SELECT query per claim, tagged with its claim_index.",
        ),
    ]
)

JUDGE_SYSTEM_PROMPT = """You are a rigorous fact-checking judge for claims about data in a structured
statistical database.

You are given a numbered list of items, each with:
1. A claim made about a specific data point or comparison (e.g. a value for a given period/category,
   or a comparison between periods/categories).
2. The exact SQL query that was executed against the authoritative database to investigate it.
3. The raw data/output retrieved by that query.

For EACH item, decide whether the retrieved data ENTAILS (supports/confirms), REFUTES
(contradicts), or is INCONCLUSIVE for its claim.

Rules:
- Base each verdict strictly on that item's own retrieved data, not on outside knowledge.
- Use "Entailed" only when the retrieved data is complete enough to fully support the claim.
- Use "Refuted" only when the retrieved data is complete enough to directly contradict the claim.
- Use "Inconclusive" whenever the retrieved data is empty, or is missing a piece needed to settle
  the claim either way - for example, a claim comparing two periods where the query only retrieved
  one of them. Do not guess Entailed or Refuted when the data does not actually settle the
  question; say Inconclusive and explain exactly what evidence is missing.
- Reference the actual numbers from the retrieved data in your reasoning.
- Return exactly one verdict per item, tagging each with its original claim_index.
"""

JUDGE_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", JUDGE_SYSTEM_PROMPT),
        (
            "human",
            "Items:\n{items_block}\n\n"
            "Does the retrieved data entail or refute each item's claim?",
        ),
    ]
)


class IndexedSqlQuery(BaseModel):
    """One SQL query tagged with the index of the claim it answers."""
    claim_index: int = Field(..., description="Index of the claim this query answers, matching the input list.")
    sql_query: str = Field(..., description="A single read-only SQL SELECT query.")


class BatchSqlQueries(BaseModel):
    """Structured output schema enforced on the batched Text-to-SQL generation call."""
    queries: List[IndexedSqlQuery] = Field(
        ..., description="One entry per input claim, each tagged with its claim_index."
    )


class IndexedVerdict(BaseModel):
    """One verdict tagged with the index of the claim it judges."""
    claim_index: int = Field(..., description="Index of the claim this verdict is for, matching the input list.")
    status: Literal["Entailed", "Refuted", "Inconclusive"] = Field(
        ...,
        description=(
            "Whether the retrieved SQL data supports (Entailed), contradicts (Refuted), or is "
            "missing the evidence needed to decide either way (Inconclusive) for the claim."
        ),
    )
    reasoning: str = Field(..., description="Concise explanation referencing the actual retrieved data.")


class BatchVerdicts(BaseModel):
    """Structured output schema enforced on the batched judge call."""
    verdicts: List[IndexedVerdict] = Field(
        ..., description="One verdict per judged item, each tagged with its claim_index."
    )


@dataclass
class _ClaimSqlState:
    """Per-claim progress through SQL generation/execution (and, later, judging)."""
    claim: str
    sql_query: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None


# Substrings that mark a claim as a comparison/ranking/trend/superlative rather than a single
# point-in-time fact. The SQL-generation prompt already instructs the model to retrieve every row
# needed for these claims, but that instruction isn't always followed - this is a deterministic
# backstop, not a replacement for it.
_COMPARISON_KEYWORDS = (
    "tertinggi", "terendah", "paling tinggi", "paling rendah", "paling besar", "paling kecil",
    "lebih tinggi", "lebih rendah", "lebih besar", "lebih kecil",
    "naik dari", "turun dari", "meningkat dari", "menurun dari",
    "dibandingkan", "dibanding", "ketimbang", "tren", "trend",
)


def _looks_like_comparison_claim(claim: str) -> bool:
    lowered = claim.lower()
    return any(keyword in lowered for keyword in _COMPARISON_KEYWORDS)


def _parse_result_rows(result: Optional[str]) -> Optional[list]:
    """Parse a `db.run()` result string (the Python repr of a list of row-tuples) back into rows.

    Returns `None` if `result` isn't in that format, so callers can fall back to not second-guessing
    an unparseable result instead of crashing on it.
    """
    if not result:
        return []
    try:
        parsed = ast.literal_eval(result)
    except Exception:
        return None
    return parsed if isinstance(parsed, list) else None


def _result_is_insufficient_for_comparison(result: Optional[str]) -> bool:
    """True when a comparison/superlative claim's query result can't actually support a comparison.

    A comparison needs at least two distinct values. Those can arrive as multiple rows (one
    period/category per row) OR as multiple non-null columns within a single row - the prompt
    above explicitly tells the model it may answer a comparison with a self-join/CASE-pivot query
    that aliases two periods into one row (e.g. `value_a, value_b`), so a bare "only one row" check
    would wrongly flag that valid shape. A single row is only insufficient when it doesn't itself
    carry at least two non-null values; a result is insufficient outright when every value in it is
    NULL (e.g. an AVG() whose WHERE clause matched zero underlying rows).
    """
    rows = _parse_result_rows(result)
    if rows is None:
        return False
    if not rows:
        return True
    flat_values = [value for row in rows for value in (row if isinstance(row, tuple) else (row,))]
    non_null_values = [value for value in flat_values if value is not None]
    if not non_null_values:
        return True
    if len(rows) == 1 and len(non_null_values) < 2:
        return True
    return False


def build_sql_chain(llm: BaseChatModel) -> Runnable:
    """Build a reusable batched Text-to-SQL chain: (schema, claims) -> BatchSqlQueries."""
    return SQL_GENERATION_PROMPT | llm.with_structured_output(BatchSqlQueries)


def build_judge_chain(llm: BaseChatModel) -> Runnable:
    """Build a reusable batched judge chain that issues one verdict per item."""
    return JUDGE_PROMPT | llm.with_structured_output(BatchVerdicts)


def generate_and_run_sql_batch(
    claims: List[str], db: SQLDatabase, sql_chain: Runnable
) -> List[_ClaimSqlState]:
    """Generate and execute one SQL query per claim, retrying every failed claim in one batch call.

    The schema is fetched once and reused for both the initial batch call and the retry batch call,
    rather than once per claim. Claims whose query is missing from the model's response, whose query
    fails to execute, or whose result doesn't carry enough data despite the claim being a comparison/
    superlative (see `_looks_like_comparison_claim`/`_result_is_insufficient_for_comparison`) are
    retried together in a single follow-up call. The retry's result is checked against the same
    insufficiency test before being accepted, so a second wrong guess doesn't silently pass through
    as if it were good data; claims that still fail after that are left with `error` set instead of
    raising, so one bad claim never blocks the rest of the document's report.
    """
    states = [_ClaimSqlState(claim=claim) for claim in claims]
    schema = db.get_table_info()
    distinct_values = get_distinct_value_hints(db)

    claims_block = "\n".join(f"{i}. {claim}" for i, claim in enumerate(claims))
    try:
        response: BatchSqlQueries = sql_chain.invoke(
            {"schema": schema, "distinct_values": distinct_values, "claims_block": claims_block, "error_context": ""}
        )
    except Exception as exc:
        logger.exception("Batch SQL generation failed")
        for state in states:
            state.error = f"Failed to generate a SQL query: {exc}"
        return states

    for item in response.queries:
        if 0 <= item.claim_index < len(states):
            states[item.claim_index].sql_query = item.sql_query

    for i, state in enumerate(states):
        if state.sql_query is None:
            state.error = "No SQL query was generated for this claim."
            continue
        try:
            state.result = db.run(state.sql_query)
        except Exception as exc:
            state.error = str(exc)
            continue
        if _looks_like_comparison_claim(state.claim) and _result_is_insufficient_for_comparison(state.result):
            state.error = (
                "This claim compares values across multiple periods/categories, but the query "
                "result doesn't contain enough data to support that comparison (e.g. it's empty, "
                "all NULL, or only a single usable value) - broaden the query, or fix the WHERE "
                "filters, to retrieve every value needed for the comparison."
            )

    failed_indices = [i for i, state in enumerate(states) if state.error is not None]
    if not failed_indices:
        return states

    logger.warning("Retrying %d claim(s) with failed SQL queries in one batch call", len(failed_indices))
    retry_claims_block = "\n".join(f"{i}. {states[i].claim}" for i in failed_indices)
    retry_error_lines = "\n".join(
        f"claim_index {i}: previous query = {states[i].sql_query!r}, error = {states[i].error}"
        for i in failed_indices
    )
    error_context = f"The following queries failed and must be fixed:\n{retry_error_lines}\n"

    try:
        retry_response: BatchSqlQueries = sql_chain.invoke(
            {"schema": schema, "distinct_values": distinct_values, "claims_block": retry_claims_block, "error_context": error_context}
        )
    except Exception as exc:
        logger.exception("Batch SQL retry generation failed")
        for i in failed_indices:
            states[i].error = f"Failed to generate a working SQL query after 2 attempts: {exc}"
        return states

    retried_queries = {item.claim_index: item.sql_query for item in retry_response.queries}
    for i in failed_indices:
        new_query = retried_queries.get(i)
        if new_query is None:
            states[i].error = "Failed to generate a working SQL query after 2 attempts."
            continue
        states[i].sql_query = new_query
        try:
            states[i].result = db.run(new_query)
        except Exception as exc:
            states[i].error = f"Failed to generate a working SQL query after 2 attempts. Last error: {exc}"
            continue
        if _looks_like_comparison_claim(states[i].claim) and _result_is_insufficient_for_comparison(states[i].result):
            states[i].error = (
                "Failed to generate a working SQL query after 2 attempts: the retried query still "
                "didn't retrieve enough data to support this comparison claim."
            )
            continue
        states[i].error = None

    return states


def judge_batch(states: List[_ClaimSqlState], judge_chain: Runnable) -> Dict[int, IndexedVerdict]:
    """Issue one verdict per claim that has usable SQL data, in a single LLM call."""
    usable_indices = [i for i, state in enumerate(states) if state.error is None]
    if not usable_indices:
        return {}

    items_block = "\n\n".join(
        f"{i}. Claim: {states[i].claim}\n"
        f"   SQL Query Used: {states[i].sql_query}\n"
        f"   Raw Data Retrieved: {states[i].result}"
        for i in usable_indices
    )

    response: BatchVerdicts = judge_chain.invoke({"items_block": items_block})
    return {verdict.claim_index: verdict for verdict in response.verdicts}


def verify_claims(
    claims: List[str], db: SQLDatabase, sql_chain: Runnable, judge_chain: Runnable
) -> List[ClaimVerificationResult]:
    """Run the batched Text-to-SQL + LLM-as-a-Judge pipeline for a whole document's claims.

    Always returns one result per input claim, in order - claims whose SQL or judge step failed
    get status "Error" with the failure reason instead of raising, so the rest of the document's
    report is unaffected.
    """
    if not claims:
        return []

    states = generate_and_run_sql_batch(claims, db, sql_chain)

    try:
        verdicts = judge_batch(states, judge_chain)
        judge_error: Optional[str] = None
    except Exception as exc:
        logger.exception("Batch judge evaluation failed")
        verdicts = {}
        judge_error = str(exc)

    results = []
    for i, state in enumerate(states):
        if state.error is not None:
            results.append(
                ClaimVerificationResult(
                    claim=state.claim,
                    status="Error",
                    sql_query_used=None,
                    reasoning=f"Verification failed: {state.error}",
                )
            )
            continue

        verdict = verdicts.get(i)
        if verdict is None:
            reasoning = judge_error or "The judge did not return a verdict for this claim."
            results.append(
                ClaimVerificationResult(
                    claim=state.claim,
                    status="Error",
                    sql_query_used=state.sql_query,
                    reasoning=f"Verification failed: {reasoning}",
                )
            )
            continue

        results.append(
            ClaimVerificationResult(
                claim=state.claim,
                status=verdict.status,
                sql_query_used=state.sql_query,
                reasoning=verdict.reasoning,
            )
        )

    return results


def verify_claim(claim: str, db: SQLDatabase, sql_chain: Runnable, judge_chain: Runnable) -> VerifyClaimResponse:
    """Verify a single ad-hoc claim (used by the single-claim API endpoint).

    Thin wrapper around `verify_claims` with a one-item list, so the single-claim and
    document-verification paths share the same batching logic instead of duplicating it.
    """
    result = verify_claims([claim], db, sql_chain, judge_chain)[0]
    if result.status == "Error":
        raise RuntimeError(result.reasoning)
    return VerifyClaimResponse(
        status=result.status,
        sql_query_used=result.sql_query_used,
        reasoning=result.reasoning,
    )
