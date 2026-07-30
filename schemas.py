"""Shared Pydantic request/response models for the fact-checking API."""

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Paired PDF + Excel verification
# ---------------------------------------------------------------------------


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


class PeriodResult(BaseModel):
    """One data point used to compute a FactVerificationResult.

    Temporal points carry (year, month); categorical points (non-time-series tables,
    e.g. an item list) carry col_label instead and leave year/month null.
    """
    metric_label: str
    year: Optional[int] = None
    month: Optional[str] = None
    col_label: Optional[str] = None
    excel_value: Optional[float] = None


class SourceValue(BaseModel):
    """What ONE reference source says about a claim, when more than one could answer it."""
    source: str                       # "filename / sheet" — the source's display label
    origin: Literal["excel", "pdf"] = "excel"
    matched_label: Optional[str] = None   # the row label this source resolved the claim against
    computed_value: Optional[float] = None
    computed_unit: Optional[str] = None
    verdict: Literal["Entailed", "Refuted", "Inconclusive"]


class FactVerificationResult(BaseModel):
    operation: Literal[
        "value", "yoy_growth", "average", "sum", "diff", "ratio",
        "is_increasing", "is_decreasing", "is_stable",
        "above_threshold", "below_threshold",
    ]
    metric_label: str  # display label - shared metric name, or "A / B" for cross-metric ops
    matched_excel_source: Optional[str] = None  # "filename / sheet" of the source that matched
    periods: List[PeriodResult] = Field(default_factory=list)
    claimed_value: Optional[float] = None
    claimed_unit: Optional[str] = None
    computed_value: Optional[float] = None
    computed_unit: Optional[str] = None
    delta: Optional[float] = None
    verdict: Literal["Entailed", "Refuted", "Inconclusive"]
    reasoning: str
    context_quote: str
    page_number: Optional[int] = None
    # "pointer" when the value was located by the tier-4 AI cell-pointer pass (the LLM
    # pointed at a coordinate; the number itself was read from the cell by code). The
    # cell references are appended to `reasoning`. None for normal table lookups.
    resolved_via: Optional[str] = None
    # Every source that could answer this claim, best-matching first — populated ONLY when
    # more than one could. The headline verdict above comes from the first entry.
    source_values: List[SourceValue] = Field(default_factory=list)
    # Set when those sources disagree beyond MATCH_TOLERANCE in the same unit:
    #   "internal" — two tables inside the PDF contradict each other (the report is
    #                internally inconsistent, regardless of whether the claim itself is right)
    #   "cross"    — a table in the PDF and an uploaded Excel sheet disagree (out of sync)
    # Deliberately NOT a fourth `verdict` value: a conflict is orthogonal to whether the claim
    # matches its best source, and entailed+refuted+inconclusive must keep summing to total_facts.
    source_conflict: Optional[Literal["internal", "cross"]] = None


class TypoIssue(BaseModel):
    word: str
    start: int
    end: int
    category: Literal["ejaan", "tidak_baku", "grammar"]
    suggestion: str
    explanation: str
    page_number: Optional[int] = None


class TypoCheckResponse(BaseModel):
    total_issues: int
    ejaan_count: int
    tidak_baku_count: int
    grammar_count: int
    summary: str
    issues: List[TypoIssue]


class TableSuggestion(BaseModel):
    """A BI table family that likely contains the data for claims found Inconclusive."""
    table: str            # human-readable table family, e.g. "Uang Primer (M0) — SEKI Tabel I.2"
    metrics: List[str]    # the inconclusive metric labels that point at this family


class PairedVerificationResponse(BaseModel):
    pdf_filename: str
    # These four are positional parallel arrays, one entry per reference source. Note that in
    # "internal"/"both" mode a source can be a table transcribed from the PDF itself, in which
    # case excel_filenames repeats the PDF's name and excel_sheets holds "Hal. 7 · Lampiran 1…".
    # The "excel_" prefix is historical; renaming it would break the UI's saved filters.
    excel_filenames: List[str]
    excel_sheets: List[str]
    excel_units: List[str]
    # Which cascade tier parsed each source: "bi" | "generic" | "llm" | "pointer-only"
    # (aligned with excel_filenames), prefixed "pdf-" for sources transcribed out of the PDF.
    # "llm" means the structure was LLM-mapped; "pointer-only" means no parser understood the
    # sheet and claims are resolved by the tier-4 cell-pointer pass — both worth a glance.
    excel_parsers: List[str] = Field(default_factory=list)
    # Which reference pool was used: "excel" (uploaded workbooks), "internal" (tables inside
    # the PDF), or "both".
    mode: str = "excel"
    # How many results have source_conflict set — sources that contradict each other.
    conflict_count: int = 0
    total_facts: int
    entailed_count: int
    refuted_count: int
    inconclusive_count: int
    results: List[FactVerificationResult]
    typo_check: Optional[TypoCheckResponse] = None
    # Populated when Inconclusive claims match a known BI table family the user did not
    # upload — tells them WHICH statistical table would make those claims checkable.
    table_suggestions: List[TableSuggestion] = Field(default_factory=list)


class TableListResponse(BaseModel):
    tables: List[str]


class TableDataResponse(BaseModel):
    table: str
    columns: List[str]
    rows: List[List[Any]]
    total_rows: int
    limit: int
    offset: int
