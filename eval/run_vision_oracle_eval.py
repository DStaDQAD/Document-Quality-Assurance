"""Oracle eval: how faithfully the vision passes read a report, scored against its own workbook.

A BI report is published together with a workbook of the same series (SK-Juni-2026.pdf with
"Tabel Series SK Juni 2026.xlsx"). The workbook parses with no LLM at all, so it is a free,
exact answer key for everything the model reads off the PDF's pictures:

  1. APPENDIX TABLES — every cell recovered from the PDF's appendix (by the text layer or by
     vision) is looked up in the workbook sheet of the same number and compared.
  2. CHART LABELS — every data label read off a chart is looked up the same way. (Enabled once
     the chart pass exists; see pdf_chart_extraction.)

The workbook is used ONLY here. In the product the PDF's appendix is the source of truth; this
script measures whether it can be.

It calls the real vision model, so it needs a key in .env and is run by hand, not in CI:

    python -m eval.run_vision_oracle_eval
    python -m eval.run_vision_oracle_eval --pdf sample_data/SK-Juni-2026.pdf \\
        --excel "sample_data/Tabel Series SK Juni 2026.xlsx" --show 20
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_PDF = "sample_data/SK-Juni-2026.pdf"
_DEFAULT_EXCEL = "sample_data/Tabel Series SK Juni 2026.xlsx"

# Two readings of one printed 1-decimal number agree when they differ by less than this.
_TOLERANCE = 0.051


def _norm_part(text: str) -> str:
    text = re.sub(r"^\s*(?:[\-–—•·*]+|(?:[A-Za-z]\d{0,2}|\d{1,2})[.)])\s*", "", str(text))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _norm_label(label: str) -> str:
    from table_model import QUAL_SEP
    return ">".join(_norm_part(p) for p in label.split(QUAL_SEP))


def _sheet_for(caption: str, sheets: List[str]) -> Optional[str]:
    """The workbook sheet with the same table number as the caption ('Tabel 2 …' -> 'Tabel 2')."""
    match = re.match(r"\s*(tabel|lampiran)\s*([ivx\d]+)", caption or "", re.IGNORECASE)
    if not match:
        return None
    want = f"{match.group(1)}{match.group(2)}".lower()
    for sheet in sheets:
        if re.sub(r"[^a-z0-9]", "", sheet.lower()) == want:
            return sheet
    return None


class _Oracle:
    """The workbook, parsed, with a label index that tolerates enumeration and bullets."""

    def __init__(self, excel_bytes: bytes):
        from excel_parser_bi import list_sheet_names
        from paired_verifier import _parse_table_with_fallback

        self.sheets = list_sheet_names(excel_bytes)
        self.tables = {}
        for sheet in self.sheets:
            try:
                self.tables[sheet], _ = _parse_table_with_fallback(excel_bytes, sheet, llm=None)
            except ValueError:
                pass

    def label_in(self, sheet: str, label: str) -> Optional[str]:
        from table_model import QUAL_SEP
        table = self.tables.get(sheet)
        if table is None:
            return None
        want = _norm_label(label)
        exact = [l for l in table.row_labels if _norm_label(l) == want]
        if len(exact) == 1:
            return exact[0]
        # A PDF row left unqualified because its name is unique in the PDF table.
        leaf = _norm_part(label.split(QUAL_SEP)[-1])
        by_leaf = [l for l in table.row_labels if _norm_part(l.split(QUAL_SEP)[-1]) == leaf]
        return by_leaf[0] if len(by_leaf) == 1 else None

    def value(self, sheet: str, label: str, year: int, month: str) -> Optional[float]:
        row = self.label_in(sheet, label)
        return None if row is None else self.tables[sheet].lookup(row, year, month)


def score_tables(pdf_tables, oracle: _Oracle, show: int) -> Dict[str, Tuple[int, int, int]]:
    """Per caption: (right, wrong, unmatched) over every temporal cell the PDF table yielded."""
    from paired_verifier import _parse_grid_with_fallback

    per: Dict[str, Tuple[int, int, int]] = {}
    shown = 0
    for pdf_table in pdf_tables:
        sheet = _sheet_for(pdf_table.caption, oracle.sheets)
        if sheet is None:
            continue
        try:
            table, _ = _parse_grid_with_fallback(pdf_table.grid, llm=None)
        except ValueError as exc:
            print(f"  {pdf_table.label}: grid did not parse ({exc})")
            continue
        right = wrong = unmatched = 0
        for key, value in table._data.items():
            if len(key) != 3:
                continue
            label, year, month = key
            expected = oracle.value(sheet, label, year, month)
            if expected is None:
                unmatched += 1
            elif abs(expected - value) < _TOLERANCE:
                right += 1
            else:
                wrong += 1
                if shown < show:
                    shown += 1
                    print(f"  WRONG {pdf_table.label} | {label} {month} {year}: "
                          f"read {value}, workbook {expected}")
        r0, w0, u0 = per.get(pdf_table.caption, (0, 0, 0))
        per[pdf_table.caption] = (r0 + right, w0 + wrong, u0 + unmatched)
    return per


def score_charts(readings, sources, show: int, heading: str) -> Dict[str, int]:
    """Every chart label run through the verifier against `sources`; verdict counts.

    Checked with the same engine the product uses (chart_label_facts + _evaluate_fact), so a
    label counts as right only if it was read right, placed on the right bar and period, AND its
    series name resolved to the right row.
    """
    from paired_verifier import _chart_row_names, check_chart_labels

    counts = {"Entailed": 0, "Refuted": 0, "Inconclusive": 0}
    shown = 0
    print(f"  -- against {heading}")
    row_names = _chart_row_names(readings, sources)
    unplaced = sorted({f"{r.indicator} | {p.series}" for r in readings for p in r.points
                       if (r.indicator, p.series) not in row_names})
    if unplaced:
        print(f"    series with no table row: {unplaced}")
    for fact, result in check_chart_labels(readings, sources, row_names):
        counts[result.verdict] += 1
        if result.verdict != "Entailed" and shown < show:
            shown += 1
            print(f"    {result.verdict:12s} {fact.context_quote[:90]}")
            print(f"                 -> {result.reasoning[:220]}")
    total = sum(counts.values())
    print(f"    labels {total}: Entailed {counts['Entailed']}  Refuted {counts['Refuted']}  "
          f"Inconclusive {counts['Inconclusive']}")
    return counts


async def _main(args) -> int:
    import logging
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
    if args.verbose:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        logging.getLogger("fact-checker").setLevel(logging.INFO)
    from llm_provider import get_vision_llm
    from pdf_table_extraction import extract_tables_from_pdf

    pdf_bytes = (_REPO_ROOT / args.pdf).read_bytes()
    oracle = _Oracle((_REPO_ROOT / args.excel).read_bytes())
    vision_llm = get_vision_llm()

    print(f"== Appendix tables: {args.pdf} vs {args.excel}")
    tables = await extract_tables_from_pdf(pdf_bytes, vision_llm)
    per = score_tables(tables, oracle, args.show)
    total = [0, 0, 0]
    for caption, (right, wrong, unmatched) in per.items():
        cells = right + wrong
        rate = f"{right / cells:.1%}" if cells else "-"
        print(f"  {caption[:60]:60s} right {right:5d}  wrong {wrong:4d}  "
              f"unmatched {unmatched:4d}  accuracy {rate}")
        total = [total[0] + right, total[1] + wrong, total[2] + unmatched]
    cells = total[0] + total[1]
    print(f"  TOTAL right {total[0]}  wrong {total[1]}  unmatched {total[2]}  "
          f"accuracy {total[0] / cells:.2%}" if cells else "  TOTAL: no comparable cell")

    from paired_verifier import _ExcelSource, _pdf_table_source
    from pdf_chart_extraction import extract_charts_from_pdf

    print()
    print(f"== Chart labels: {args.pdf}")
    readings = await extract_charts_from_pdf(pdf_bytes, vision_llm)
    print(f"  {len(readings)} chart(s), {sum(len(r.points) for r in readings)} label(s)")
    workbook = [_ExcelSource(table=t, filename=args.excel, sheet=sh)
                for sh, t in oracle.tables.items()]
    score_charts(readings, workbook, args.show, "the workbook (oracle)")
    appendix = [_pdf_table_source(t, args.pdf, "pdf", None)[0] for t in tables]
    score_charts(readings, appendix, args.show, "the PDF's own appendix (product path)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pdf", default=_DEFAULT_PDF)
    parser.add_argument("--excel", default=_DEFAULT_EXCEL)
    parser.add_argument("--show", type=int, default=15, help="wrong cells to print")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log what the extraction kept and dropped")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO_ROOT))
    sys.exit(main())
