"""Per-publication Excel parsers — one module per BI publication.

Each module declares SHEET_PATTERNS (regexes matched against the whole, stripped sheet name,
case-insensitive) and parse(data, sheet_name) -> TableData, raising PublicationParseError when
a covered sheet does not have the layout it expects. paired_verifier tries the parser of the
publication the checker picked before its generic cascade (see _parse_table_with_fallback).
Design: docs/superpowers/specs/2026-10-07-publication-parsers-design.md
"""
from types import ModuleType
from typing import Dict, Optional

from publication_parsers import cadangan_devisa, npi, pii, pmi, ppkom, sbank, shpr, sk, skdu, spe, sulni, uang_beredar, uang_primer
from publication_parsers._common import PublicationParseError, load_grid, sheet_matches
from table_model import TableData

PARSERS: Dict[str, ModuleType] = {
    "uang-beredar": uang_beredar,
    "uang-primer-m0": uang_primer,
    "cadangan-devisa": cadangan_devisa,
    "sulni": sulni,
    "npi": npi,
    "pii": pii,
    "sk": sk,
    "skdu": skdu,
    "spe": spe,
    "sbank": sbank,
    "shpr": shpr,
    "pmi": pmi,
    "ppkom": ppkom,
}

__all__ = [
    "PARSERS", "PublicationParseError", "covers", "load_grid", "parse_for_publication",
    "skipped_sections",
]


def covers(publication: str, sheet_name: str) -> bool:
    """True when `publication` has a parser and it claims this sheet."""
    module = PARSERS.get(publication or "")
    return module is not None and any(
        sheet_matches(pattern, sheet_name) for pattern in module.SHEET_PATTERNS
    )


def parse_for_publication(publication: str, data: bytes, sheet_name: str) -> Optional[TableData]:
    """The publication's own reading of the sheet; None when it has no parser for this sheet."""
    if not covers(publication, sheet_name):
        return None
    return PARSERS[publication].parse(data, sheet_name)


def skipped_sections(publication: str) -> tuple:
    """Headings (regexes on the whole heading line) of report sections the extractor is not shown:
    they hold no claim about the data, only figures that look like one (SBank's METODOLOGI:
    "Nilai SBT > 0 berarti lebih ketat")."""
    module = PARSERS.get(publication or "")
    return tuple(getattr(module, "SKIPPED_SECTIONS", ()))
