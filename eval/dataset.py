"""Labelled-case schema and loader for the Layer-1 comparison eval.

A case is a self-contained unit: one or more inline reference tables (real BI numbers,
frozen into the YAML so the eval needs neither the .xls binaries nor xlrd at runtime), one
fully specified extracted fact, and the verdict a correct verifier should return. A case
that lists several tables also scores whether the engine reported them as contradicting
each other. Building the
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
from table_model import TableData


@dataclass(frozen=True)
class CellSpec:
    """One inline table cell. Temporal cells carry year+month, categorical ones col_label."""
    label: str
    value: float
    year: Optional[int] = None
    month: Optional[str] = None
    col_label: Optional[str] = None


@dataclass(frozen=True)
class PeriodSpec:
    metric_label: str
    year: Optional[int] = None
    month: Optional[str] = None
    col_label: Optional[str] = None


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
    # "internal" | "cross" | None. None means the sources are expected to AGREE, so a case
    # that silently starts conflicting fails instead of passing on its verdict alone.
    source_conflict: Optional[str] = None


@dataclass(frozen=True)
class TableSpec:
    """One reference source: an inline table plus the identity it is reported under."""
    title: str = ""
    unit: str = ""
    filename: str = "eval_table.xls"
    sheet: str = "I.1"
    # "excel" (an uploaded workbook sheet) | "pdf" (a table transcribed from the report
    # itself). Two disagreeing pdf sources are an "internal" conflict, anything else "cross".
    origin: str = "excel"
    cells: List[CellSpec] = field(default_factory=list)
    axis: str = "temporal"  # "temporal" | "categorical" - derived from the cells


@dataclass(frozen=True)
class ComparisonCase:
    id: str
    fact: FactSpec
    expected: Expected
    description: str = ""
    tables: List[TableSpec] = field(default_factory=list)


def _cells_axis(cells: List[CellSpec], case_id: str) -> str:
    """Which column axis the inline table uses, derived from its cells.

    A table is temporal (year+month keys) or categorical (col_label keys), never both:
    _evaluate_fact resolves a data point only against a source of the matching axis kind,
    so a mixed table would silently make half its cells unreachable.
    """
    kinds = {"categorical" if c.col_label is not None else "temporal" for c in cells}
    if len(kinds) > 1:
        raise ValueError(
            f"Case {case_id!r}: table mixes temporal (year/month) and categorical (col_label) cells"
        )
    axis = kinds.pop() if kinds else "temporal"
    if axis == "temporal":
        for cell in cells:
            if cell.year is None or cell.month is None:
                raise ValueError(
                    f"Case {case_id!r}: temporal cell {cell.label!r} needs both year and month "
                    f"(got year={cell.year!r}, month={cell.month!r})"
                )
    return axis


def _parse_table(raw_table: dict, case_id: str) -> TableSpec:
    cells = [CellSpec(**c) for c in raw_table.get("data", [])]
    return TableSpec(
        title=raw_table.get("title", ""),
        unit=raw_table.get("unit", ""),
        filename=raw_table.get("filename", "eval_table.xls"),
        sheet=raw_table.get("sheet", "I.1"),
        origin=raw_table.get("origin", "excel"),
        cells=cells,
        axis=_cells_axis(cells, case_id),
    )


def _parse_case(raw: dict) -> ComparisonCase:
    # `table:` (one source) and `tables:` (several) are the same thing to the engine, which
    # always takes a list; the singular form is kept because most cases need only one.
    raw_tables = raw.get("tables")
    if raw_tables is None:
        raw_tables = [raw.get("table", {}) or {}]
    elif not isinstance(raw_tables, list):
        raise ValueError(
            f"Case {raw['id']!r}: 'tables' must be a list, got {type(raw_tables).__name__}"
        )
    tables = [_parse_table(t, raw["id"]) for t in raw_tables]

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
        source_conflict=exp_raw.get("source_conflict"),
    )

    return ComparisonCase(
        id=raw["id"],
        fact=fact,
        expected=expected,
        description=raw.get("description", ""),
        tables=tables,
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


def _build_source(spec: TableSpec) -> _ExcelSource:
    """Materialise one inline table into a live _ExcelSource for _evaluate_fact."""
    if spec.axis == "categorical":
        table = TableData(
            title=spec.title, unit=spec.unit,
            row_labels=[], col_labels=[], axis_type="categorical",
        )
    else:
        table = BITableData(title=spec.title, unit=spec.unit, row_labels=[])
    for cell in spec.cells:
        if cell.label not in table.row_labels:
            table.row_labels.append(cell.label)
        # First occurrence wins for duplicate keys, mirroring parse_bi_table.
        if spec.axis == "categorical":
            if cell.col_label not in table.col_labels:
                table.col_labels.append(cell.col_label)
            table._data.setdefault((cell.label, cell.col_label), cell.value)
        else:
            table._data.setdefault((cell.label, cell.year, cell.month), cell.value)
    return _ExcelSource(
        table=table, filename=spec.filename, sheet=spec.sheet, origin=spec.origin
    )


def build_sources(case: ComparisonCase) -> List[_ExcelSource]:
    """Every reference source a case offers, in declaration order (the engine ranks them)."""
    return [_build_source(spec) for spec in case.tables]


def build_fact(case: ComparisonCase) -> ExtractedFact:
    """Materialise a case's fact spec into the ExtractedFact _evaluate_fact expects."""
    periods = [
        PeriodPoint(metric_label=p.metric_label, year=p.year, month=p.month, col_label=p.col_label)
        for p in case.fact.periods
    ]
    return ExtractedFact(
        operation=case.fact.operation,
        periods=periods,
        claimed_value=case.fact.claimed_value,
        unit=case.fact.unit,
        context_quote=case.fact.context_quote,
        page_number=None,
    )
