"""Shared Pydantic request/response models for the fact-checking API."""

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


class ClaimRequest(BaseModel):
    claim: str = Field(..., min_length=1, description="The factual claim to verify against the database.")


class DocumentRequest(BaseModel):
    document: str = Field(..., min_length=1, description="Long-form text to scan for checkable factual claims.")


class VerifyClaimResponse(BaseModel):
    status: Literal["Entailed", "Refuted", "Inconclusive"]
    sql_query_used: str
    reasoning: str


class ClaimVerificationResult(BaseModel):
    claim: str
    status: Literal["Entailed", "Refuted", "Inconclusive", "Error"]
    sql_query_used: Optional[str] = None
    reasoning: str


class VerifyDocumentResponse(BaseModel):
    total_claims: int
    entailed_count: int
    refuted_count: int
    inconclusive_count: int
    error_count: int
    summary: str
    results: List[ClaimVerificationResult]


class UploadExcelSourceResponse(BaseModel):
    filename: str
    n_sheets: int
    n_facts: int
    auto_aggregate: int
    auto_not_aggregate: int
    llm_escalated: int
    defaulted: int


class TableListResponse(BaseModel):
    tables: List[str]


class TableDataResponse(BaseModel):
    table: str
    columns: List[str]
    rows: List[List[Any]]
    total_rows: int
    limit: int
    offset: int
