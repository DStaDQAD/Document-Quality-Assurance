"""Labelled-case schema and loader for the Layer-1 comparison eval.

A case is a self-contained unit: an inline Excel table (real BI numbers, frozen into
the YAML so the eval needs neither the .xls binaries nor xlrd at runtime), one fully
specified extracted fact, and the verdict a correct verifier should return. Building the
table and the fact here — rather than referencing live files — keeps Layer 1 fully
deterministic and reproducible, which is the whole point of separating it from the
LLM-in-the-loop Layer 2.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource
from structured_extractor import ExtractedFact, PeriodPoint


@dataclass(frozen=True)
class CellSpec:
    label: str
    year: int
    month: str
    value: float


@dataclass(frozen=True)
class PeriodSpec:
    metric_label: str
    year: int
    month: str


@dataclass(frozen=True)
class FactSpec:
    operation: str
    periods: List[PeriodSpec]
    claimed_value: Optional[float] = None
    unit: Optional[str] = None
    context_quote: str = "(eval)"


@dataclass(frozen=True)
class Expected:
    verdict: str
    computed_value: Optional[float] = None  # optional cross-check of the computed number


@dataclass(frozen=True)
class ComparisonCase:
    id: str
    fact: FactSpec
    expected: Expected
    description: str = ""
    table_title: str = ""
    table_unit: str = ""
    cells: List[CellSpec] = field(default_factory=list)
    source_filename: str = "eval_table.xls"
    source_sheet: str = "I.1"


def _parse_case(raw: dict) -> ComparisonCase:
    table = raw.get("table", {}) or {}
    cells = [CellSpec(**c) for c in table.get("data", [])]

    fact_raw = raw["fact"]
    periods = [PeriodSpec(**p) for p in fact_raw["periods"]]
    fact = FactSpec(
        operation=fact_raw["operation"],
        periods=periods,
        claimed_value=fact_raw.get("claimed_value"),
        unit=fact_raw.get("unit"),
        context_quote=fact_raw.get("context_quote", "(eval)"),
    )

    exp_raw = raw["expected"]
    expected = Expected(
        verdict=exp_raw["verdict"],
        computed_value=exp_raw.get("computed_value"),
    )

    return ComparisonCase(
        id=raw["id"],
        fact=fact,
        expected=expected,
        description=raw.get("description", ""),
        table_title=table.get("title", ""),
        table_unit=table.get("unit", ""),
        cells=cells,
        source_filename=table.get("filename", "eval_table.xls"),
        source_sheet=table.get("sheet", "I.1"),
    )


def load_cases(paths: List[Path]) -> List[ComparisonCase]:
    """Load every case from the given YAML files/globs (each file holds a list of cases).

    Raises ValueError on a duplicate case id so the report never conflates two cases.
    """
    cases: List[ComparisonCase] = []
    seen: Dict[str, str] = {}
    for path in paths:
        raw_docs = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(raw_docs, list):
            raise ValueError(f"{path}: expected a top-level list of cases, got {type(raw_docs).__name__}")
        for raw in raw_docs:
            case = _parse_case(raw)
            if case.id in seen:
                raise ValueError(f"Duplicate case id {case.id!r} in {path} (already in {seen[case.id]})")
            seen[case.id] = str(path)
            cases.append(case)
    return cases


def build_source(case: ComparisonCase) -> _ExcelSource:
    """Materialise a case's inline table into a live _ExcelSource for _evaluate_fact."""
    table = BITableData(title=case.table_title, unit=case.table_unit, row_labels=[])
    for cell in case.cells:
        if cell.label not in table.row_labels:
            table.row_labels.append(cell.label)
        # First occurrence wins for duplicate keys, mirroring parse_bi_table.
        table._data.setdefault((cell.label, cell.year, cell.month), cell.value)
    return _ExcelSource(table=table, filename=case.source_filename, sheet=case.source_sheet)


def build_fact(case: ComparisonCase) -> ExtractedFact:
    """Materialise a case's fact spec into the ExtractedFact _evaluate_fact expects."""
    periods = [PeriodPoint(metric_label=p.metric_label, year=p.year, month=p.month) for p in case.fact.periods]
    return ExtractedFact(
        operation=case.fact.operation,
        periods=periods,
        claimed_value=case.fact.claimed_value,
        unit=case.fact.unit,
        context_quote=case.fact.context_quote,
        page_number=None,
    )
