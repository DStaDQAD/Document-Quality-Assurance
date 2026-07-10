"""Document-level labels for the Layer-2 end-to-end eval.

A Layer-2 case points at a real PDF + one or more Excel sources and lists the claims a
human expects the pipeline to extract, each with the verdict a correct verifier should
reach. Unlike Layer 1 (which hands the engine a ready-made fact), Layer 2 runs the whole
pipeline — including the LLM extraction — so it also measures whether claims are found at
all, not just scored correctly. That makes it non-deterministic and dependent on real
providers, so it is authored by a domain reviewer and run on demand, never in CI.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

from eval.dataset import PeriodSpec


@dataclass(frozen=True)
class ExpectedClaim:
    metric: str            # metric name to match against the result's label (tolerant containment)
    operation: str
    periods: List[PeriodSpec]
    expected_verdict: str  # Entailed | Refuted | Inconclusive
    note: str = ""


@dataclass(frozen=True)
class E2ECase:
    id: str
    pdf: str               # path (relative to repo root or absolute)
    excel: List[str]       # one or more Excel paths
    sheets: List[str]      # sheet per Excel file (last is reused if fewer than files)
    claims: List[ExpectedClaim]
    description: str = ""

    def sheet_for(self, index: int) -> str:
        return self.sheets[index] if index < len(self.sheets) else self.sheets[-1]


def _parse_claim(raw: dict) -> ExpectedClaim:
    return ExpectedClaim(
        metric=raw["metric"],
        operation=raw["operation"],
        periods=[PeriodSpec(**p) for p in raw["periods"]],
        expected_verdict=raw["expected_verdict"],
        note=raw.get("note", ""),
    )


def _parse_case(raw: dict) -> E2ECase:
    excel = raw["excel"]
    if isinstance(excel, str):
        excel = [excel]
    sheets = raw.get("sheets", ["I.1"])
    if isinstance(sheets, str):
        sheets = [sheets]
    return E2ECase(
        id=raw["id"],
        pdf=raw["pdf"],
        excel=excel,
        sheets=sheets,
        claims=[_parse_claim(c) for c in raw.get("claims", [])],
        description=raw.get("description", ""),
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
