"""Document-level labels for the Layer-2 end-to-end eval.

A Layer-2 case points at a real PDF + its reference tables — uploaded Excel sheets, the
tables printed inside the PDF itself (`mode: internal`), or both — and lists the claims a
human expects the pipeline to extract, each with the verdict a correct verifier should
reach. Unlike Layer 1 (which hands the engine a ready-made fact), Layer 2 runs the whole
pipeline — including the LLM extraction — so it also measures whether claims are found at
all, not just scored correctly. That makes it non-deterministic and dependent on real
providers, so it is authored by a domain reviewer and run on demand, never in CI.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from eval.dataset import PeriodSpec


@dataclass(frozen=True)
class ExpectedClaim:
    metric: str            # metric name to match against the result's label (tolerant containment)
    operation: str
    periods: List[PeriodSpec]
    expected_verdict: str  # Entailed | Refuted | Inconclusive
    note: str = ""
    # The number the PDF states, when the document states the SAME metric/period twice with
    # different figures — a doctored summary bullet against an untouched body paragraph, say.
    # Both extract to facts that are identical on metric, operation and period, so without
    # this the two labels would be told apart only by extraction order. Optional: leave it
    # out whenever the claim is unique in the document.
    claimed_value: Optional[float] = None


@dataclass(frozen=True)
class E2ECase:
    id: str
    pdf: str               # path (relative to repo root or absolute)
    excel: List[str]       # one or more Excel paths; empty in "internal" mode
    sheets: List[str]      # sheet per Excel file (last is reused if fewer than files)
    claims: List[ExpectedClaim]
    description: str = ""
    # Which reference pool the case is scored against — the same three values the endpoint
    # takes: "excel" (uploaded workbooks), "internal" (tables printed inside the PDF), "both".
    mode: str = "excel"

    def sheet_for(self, index: int) -> str:
        return self.sheets[index] if index < len(self.sheets) else self.sheets[-1]


def _parse_claim(raw: dict) -> ExpectedClaim:
    return ExpectedClaim(
        metric=raw["metric"],
        operation=raw["operation"],
        periods=[PeriodSpec(**p) for p in raw["periods"]],
        expected_verdict=raw["expected_verdict"],
        note=raw.get("note", ""),
        claimed_value=raw.get("claimed_value"),
    )


_MODES = ("excel", "internal", "both")


def _parse_case(raw: dict) -> E2ECase:
    case_id = raw["id"]
    excel = raw.get("excel", [])
    if isinstance(excel, str):
        excel = [excel]
    sheets = raw.get("sheets", ["I.1"])
    if isinstance(sheets, str):
        sheets = [sheets]
    mode = raw.get("mode", "excel")
    # Same two rules the endpoint enforces (main._validate_mode), applied at load time so a
    # mislabelled case fails before the runner spends a single LLM call on it.
    if mode not in _MODES:
        raise ValueError(f"{case_id}: unknown mode {mode!r} (expected one of {', '.join(_MODES)})")
    if mode in ("excel", "both") and not excel:
        raise ValueError(f"{case_id}: mode {mode!r} needs at least one Excel source")
    return E2ECase(
        id=case_id,
        pdf=raw["pdf"],
        excel=excel,
        sheets=sheets,
        claims=[_parse_claim(c) for c in raw.get("claims", [])],
        description=raw.get("description", ""),
        mode=mode,
    )


def load_e2e_cases(paths: List[Path]) -> List[E2ECase]:
    """Load Layer-2 cases from YAML files (each file holds a list of cases)."""
    cases: List[E2ECase] = []
    seen: set = set()
    for path in paths:
        raw_docs = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(raw_docs, list):
            raise ValueError(f"{path}: expected a top-level list of cases")
        for raw in raw_docs:
            case = _parse_case(raw)
            if case.id in seen:
                raise ValueError(f"Duplicate e2e case id {case.id!r}")
            seen.add(case.id)
            cases.append(case)
    return cases
