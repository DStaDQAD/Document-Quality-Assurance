"""Read workbook cells the way the engine parses them (no LLM), to settle a label's verdict.

usage: inv_lookup.py <manifest.yaml> <doc_id> "<row substring>" [periods like 2026:Jun,2026:Mar]
Lists matching rows of every source sheet with their values at the given periods.
"""
import logging, sys
from pathlib import Path
logging.disable(logging.WARNING)
sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from eval.regression import load_manifest
from eval.run_regression import _request_files
from paired_verifier import _parse_table_with_fallback

manifest, doc_id = sys.argv[1], sys.argv[2]
needles = [n.strip().lower() for n in sys.argv[3].split("|")]
periods = [tuple(p.split(":")) for p in sys.argv[4].split(",")] if len(sys.argv) > 4 else []
doc = next(d for d in load_manifest([Path(manifest)]) if d.id == doc_id)
files, sheets = _request_files(doc, ROOT)
only = sys.argv[5].split("|") if len(sys.argv) > 5 else None
for (_, (name, data, _c)), sheet in zip(files[1:], sheets):
    if only and sheet not in only:
        continue
    try:
        t, parser = _parse_table_with_fallback(data, sheet, llm=None, publication=doc.publication)
    except ValueError:
        continue
    rows = [r for r in t.row_labels if any(n in r.lower() for n in needles)] if needles != ["*"] else list(t.row_labels)
    if not rows:
        continue
    print(f"== {Path(name).name} / {sheet} ({t.unit}) parser={parser}")
    for r in rows:
        if periods:
            vals = []
            for y, m in periods:
                v = t._data.get((r, int(y), m))
                vals.append(f"{m} {y}={v:.4f}" if v is not None else f"{m} {y}=-")
            print(f"  {r}: " + "  ".join(vals))
        else:
            print(f"  {r}")
