"""SULNI: the three workbooks of the Statistik Utang Luar Negeri Indonesia zip.

TABEL_INDONESIA (TabI.1–I.7), TABEL_PEMERINTAH (Tbl II.1–II.6) and TABEL_SWASTA (Tbl III.1–III.10)
share one layout: labels across columns 1–5 carrying their own numbering ('2.1.1. Bank / Bank',
'a.', '-'), years in row 5 written only at each year's first column, months in row 6 with
'*' / '**' marks, and annual columns for the early years (skipped). TabI.5/I.6 put the maturity
group in columns 1–4 and the borrower in column 5. TabI.7 (indicator ratios) is quarterly with
one combined header row ('Q2-2026**') and nests by the column its label starts in.
Tbl II.7 (payment schedule) and Tbl II.8 (SBN series) are not time series: not covered.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

_LABELS = (1, 2, 3, 4, 5)

SPECS = (
    (r"TabI\.7", SheetSpec(label_cols=_LABELS, header="combined", hierarchy="first_col")),
    (r"TabI\.[1-6]|Tbl II\.[1-6]|Tbl III\.(?:[1-9]|10)",
     SheetSpec(label_cols=_LABELS, hierarchy="numbering")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
