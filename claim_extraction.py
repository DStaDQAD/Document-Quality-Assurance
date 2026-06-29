"""Extracts atomic, checkable factual claims about tabular/statistical data from free text."""

from typing import List

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

EXTRACTION_SYSTEM_PROMPT = """You are an expert at extracting verifiable factual claims from documents \
about data tracked in a structured statistical database - values broken down by period (e.g. year,
quarter, month) and/or category (e.g. product, region, indicator).

Rules:
- Only extract claims that reference a specific, checkable data point or comparison (e.g. a value for
  a given period/category, or a comparison between two periods/categories).
- Decompose compound sentences into separate atomic claims - one fact or comparison per claim.
- Rewrite each claim as a self-contained sentence: resolve pronouns and fill in any period/category that
  is only implied by surrounding context, so the claim can be verified on its own without the rest of
  the document.
- Ignore opinions, predictions, and any content that has no specific, checkable data point - this
  extractor doesn't know in advance which topics the database covers, so don't filter by topic, only
  by whether the claim is concretely checkable.
- If the document contains no checkable claims, return an empty list.
"""

EXTRACTION_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", EXTRACTION_SYSTEM_PROMPT),
        ("human", "Document:\n\n{document}\n\nExtract all checkable factual claims."),
    ]
)


class ExtractedClaims(BaseModel):
    """Structured output schema enforced on the extraction LLM call."""
    claims: List[str] = Field(
        default_factory=list,
        description="Atomic, self-contained, checkable factual claims found in the document.",
    )


def extract_claims(document: str, llm: BaseChatModel) -> List[str]:
    """Run the extraction LLM over a document and return the list of atomic claims found."""
    structured_llm = llm.with_structured_output(ExtractedClaims)
    chain = EXTRACTION_PROMPT | structured_llm
    result: ExtractedClaims = chain.invoke({"document": document})
    return [c.strip() for c in result.claims if c.strip()]
