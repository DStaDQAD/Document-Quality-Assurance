"""Regression run over a corpus of real documents, compared claim by claim.

    python -m eval.run_regression run   --manifest M.yaml [--manifest M2.yaml] --out DIR
                                        [--only id1,id2] [--data-root PATH] [--no-ai-review]
    python -m eval.run_regression diff  BASE_DIR NEW_DIR [--all]
    python -m eval.run_regression score DIR --manifest M.yaml [...]
    python -m eval.run_regression replay DIR --manifest M.yaml --out REPLAY_DIR

`run` sends every document through /api/verify-paired in-process — the same request the UI
makes — and writes DIR/<id>.json. It calls the real LLM provider (costs money) and skips a
document whose JSON is already there, so an interrupted run resumes. Tinjauan AI is on unless
--no-ai-review. To record a baseline of another commit, run it from a git worktree of that
commit and pass --data-root pointing at the checkout that holds the (gitignored) files.

`diff` and `score` read saved JSON only and are free. Same-day runs are the only fair
baseline: the extractor varies run to run (docs/audit-sep2026-next.md, rule #2).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from eval.regression import (
    Document, diff_runs, fact_from_dict, fact_from_result, fact_to_dict, load_manifest, score_label,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_SHORT = {"Entailed": "Sesuai", "Refuted": "TIDAK SESUAI", "Inconclusive": "TCD", None: "-"}
_WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _resolve(path: str, data_root: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else data_root / p


def _request_files(doc: Document, data_root: Path):
    import openpyxl

    main_path = _resolve(doc.doc, data_root)
    ctype = _WORD if main_path.suffix.lower() == ".docx" else "application/pdf"
    files = [("pdf_file", (main_path.name, main_path.read_bytes(), ctype))]
    sheets: List[str] = []
    for src in doc.sources:
        path = _resolve(src.file, data_root)
        if src.sheets == "*":
            if path.suffix.lower() == ".xls":
                import xlrd
                names = xlrd.open_workbook(str(path), on_demand=True).sheet_names()
            else:
                names = openpyxl.load_workbook(path, read_only=True).sheetnames
        else:
            names = list(src.sheets)
        data = path.read_bytes()
        for name in names:
            files.append(("excel_file", (path.name, data, "application/octet-stream")))
            sheets.append(name)
    return files, sheets


def cmd_run(args) -> int:
    # Set before main is imported: the login gate and the history DB read these at import.
    os.environ["APP_USERNAME"] = ""
    os.environ["HISTORY_DATABASE_URL"] = ""
    os.environ["AI_REVIEW"] = "" if args.no_ai_review else "1"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data_root = Path(args.data_root) if args.data_root else REPO_ROOT
    # A worktree has no .env of its own (it is gitignored); the keys live with the data.
    from dotenv import load_dotenv
    load_dotenv(data_root / ".env")

    import check_history
    check_history.LOCAL_DB_PATH = out / "history.db"     # never the production history
    from fastapi.testclient import TestClient
    import main

    docs = load_manifest(args.manifest)
    if args.only:
        wanted = set(args.only.split(","))
        docs = [d for d in docs if d.id in wanted]
    client = TestClient(main.app)
    # Keep the extracted facts with each result, so `replay` can re-judge them exactly later.
    import paired_verifier
    captured: dict = {}
    dedupe = paired_verifier._deduplicate_facts

    def _capture(facts):
        captured["facts"] = dedupe(facts)
        return captured["facts"]

    paired_verifier._deduplicate_facts = _capture
    for doc in docs:
        target = out / f"{doc.id}.json"
        if target.exists():
            print(f"[{doc.id}] sudah ada, dilewati")
            continue
        files, sheets = _request_files(doc, data_root)
        # The Tinjauan AI's raw answers, next to the result and never inside it (ai_review).
        raw_log = out / f"{doc.id}.ai_review_raw.jsonl"
        raw_log.unlink(missing_ok=True)
        os.environ["AI_REVIEW_RAW_LOG"] = str(raw_log)
        started = time.time()
        response = client.post(
            "/api/verify-paired",
            params={"run_typo_check": "false", "check_charts": "false",
                    "publication": doc.publication, "sheet_names": ",".join(sheets),
                    "mode": doc.mode},
            files=files,
        )
        body = response.json()
        if response.status_code != 200:
            print(f"[{doc.id}] GAGAL {response.status_code}: {str(body)[:300]}")
            continue
        body["_regression"] = {"seconds": round(time.time() - started), "sheets": len(sheets)}
        body["_facts"] = [fact_to_dict(f) for f in captured.pop("facts", [])]
        target.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[{doc.id}] {time.time() - started:.0f}s · {body.get('total_facts')} klaim · "
              f"{body.get('entailed_count')}/{body.get('refuted_count')}/{body.get('inconclusive_count')}")
    return 0


def _results(path: Path) -> List[dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("results", [])


def _line(r: Optional[dict]) -> str:
    if not r:
        return "-"
    value = r.get("claimed_value")
    return (f"{r.get('operation')} | {r.get('metric_label')}"
            + (f" | {value}" if value is not None else "")
            + f" | {(r.get('context_quote') or '')[:120]}")


def cmd_diff(args) -> int:
    base_dir, new_dir = Path(args.base), Path(args.new)
    flagged_total = 0
    for new_path in sorted(new_dir.glob("*.json")):
        base_path = base_dir / new_path.name
        if not base_path.exists():
            print(f"## {new_path.stem}: tidak ada di baseline")
            continue
        changes = diff_runs(_results(base_path), _results(new_path))
        counts = summarize(changes)
        flagged_total += counts["perlu_dijelaskan"]
        print(f"## {new_path.stem}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        for c in changes:
            if c.kind == "sama" and not args.all:
                continue
            before = _SHORT[c.base.get("verdict") if c.base else None]
            after = _SHORT[c.new.get("verdict") if c.new else None]
            mark = "!!" if c.needs_review else "  "
            ai = " [AI]" if c.ai_review else ""
            print(f"{mark} {c.kind:8} {before:>12} -> {after:<12}{ai} {_line(c.new or c.base)}")
    print(f"\nPerlu dijelaskan (Sesuai hilang/berubah, Tidak Sesuai baru): {flagged_total}")
    return 0


def _parsed_sources(doc: Document, data_root: Path):
    """The document's sheets parsed the way the endpoint parses them, with no LLM tier."""
    from paired_verifier import _ExcelSource, _load_grid, _parse_table_with_fallback

    files, sheets = _request_files(doc, data_root)
    sources = []
    for (_, (name, data, _ctype)), sheet in zip(files[1:], sheets):
        try:
            table, _ = _parse_table_with_fallback(data, sheet, llm=None, publication=doc.publication)
        except ValueError:
            continue
        try:
            grid = _load_grid(data, sheet)
        except ValueError:
            grid = None
        sources.append(_ExcelSource(table=table, filename=name, sheet=sheet, grid=grid))
    return sources


def _narrative_of(doc: Document, data_root: Path) -> Optional[str]:
    """The Word document's narrative as the endpoint reads it; None for a PDF (needs OCR)."""
    path = _resolve(doc.doc, data_root)
    if path.suffix.lower() != ".docx":
        return None
    from word_extraction import read_word_document
    return read_word_document(path.read_bytes()).narrative_text


def _catch_up_on_quotes(facts, narrative: str) -> None:
    """Give facts saved by older code what extraction now adds: the sentence of an anchor
    that was never found (structured_extractor._locate_by_value) and the section headings.
    Only facts whose fields are missing are touched, so newer saved facts replay as saved."""
    import re as _re
    from structured_extractor import (
        _expand_anchor_to_sentence, _filter_narrative, _locate_by_value, _normalize_ws, _page_at,
        _parse_indonesian_number, _sig_words, attach_section_headings,
    )
    normalized = _normalize_ws(_filter_narrative(narrative))
    for f in facts:
        if f.page_number is not None or f.claimed_value is None:
            continue
        printed = [t for t in _re.findall(r"\d[\d.,]*\d|\d", f.context_quote or "")
                   if (_parse_indonesian_number(t) or 0) == abs(f.claimed_value)]
        if not printed:
            continue
        words = _sig_words(" ".join(p.metric_label for p in f.periods))
        span = _locate_by_value(printed[0], normalized, words)
        if span is not None:
            f.context_quote = _expand_anchor_to_sentence(normalized, *span) or f.context_quote
            f.page_number = _page_at(normalized, span[0])
    if not any(getattr(f, "section_headings", None) for f in facts):
        attach_section_headings(facts, narrative)


def cmd_replay(args) -> int:
    """Re-judge saved facts with the CURRENT comparison code — no LLM, no cost.

    Uses the facts a run saved (`_facts`) when there are any, else rebuilds them from the saved
    results (see fact_from_result). Not the whole pipeline: no extraction, no cell pointer, no
    Tinjauan AI — so replay two commits and diff those, never a replay against a live run.
    """
    from paired_verifier import (
        _attach_group_exclusions, _attach_named_members, _attach_rankings, _drop_group_stand_ins,
        _evaluate_fact_safely, _with_reason_codes,
    )

    run_dir, out = Path(args.dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data_root = Path(args.data_root) if args.data_root else REPO_ROOT
    for doc in load_manifest(args.manifest):
        path = run_dir / f"{doc.id}.json"
        if not path.exists():
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("_facts"):
            facts, rebuilt = [fact_from_dict(f) for f in body["_facts"]], False
        else:
            facts, rebuilt = [fact_from_result(r) for r in body.get("results", [])], True
        skipped = sum(f is None for f in facts)
        facts = [f for f in facts if f is not None]
        sources = _parsed_sources(doc, data_root)
        narrative = _narrative_of(doc, data_root)
        if narrative is not None:
            _catch_up_on_quotes(facts, narrative)
        facts = _drop_group_stand_ins(facts)
        _attach_named_members(facts)
        _attach_group_exclusions(facts)
        _attach_rankings(facts)
        results = [r.model_dump() for r in _with_reason_codes([_evaluate_fact_safely(f, sources) for f in facts])]
        (out / f"{doc.id}.json").write_text(json.dumps(
            {"results": results, "_replay": {"rebuilt": rebuilt, "skipped": skipped}},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[{doc.id}] {len(results)} klaim dinilai ulang"
              + (f", {skipped} tidak bisa dibangun ulang" if skipped else "")
              + (" (dari hasil, bukan fakta tersimpan)" if rebuilt else ""))
    return 0


def cmd_score(args) -> int:
    run_dir = Path(args.dir)
    totals = {"benar": 0, "salah": 0, "hilang": 0}
    for doc in load_manifest(args.manifest):
        if not doc.labels:
            continue
        path = run_dir / f"{doc.id}.json"
        if not path.exists():
            print(f"## {doc.id}: belum dijalankan")
            continue
        results = _results(path)
        print(f"## {doc.id}")
        for label in doc.labels:
            s = score_label(label, results)
            totals[s.status] += 1
            got = ", ".join(_SHORT[v] for v in s.verdicts) or "-"
            print(f"  {s.status:6} harus {_SHORT[label.expected]:<12} dapat {got:<30} "
                  f"{label.quote[:70]}" + (f"  ({label.note})" if label.note else ""))
    print("\n" + ", ".join(f"{k} {v}" for k, v in totals.items()))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--manifest", type=Path, action="append", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--only")
    p.add_argument("--data-root")
    p.add_argument("--no-ai-review", action="store_true")
    p = sub.add_parser("diff")
    p.add_argument("base")
    p.add_argument("new")
    p.add_argument("--all", action="store_true", help="also list claims that did not change")
    p = sub.add_parser("replay")
    p.add_argument("dir")
    p.add_argument("--manifest", type=Path, action="append", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--data-root")
    p = sub.add_parser("score")
    p.add_argument("dir")
    p.add_argument("--manifest", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    return {"run": cmd_run, "diff": cmd_diff, "score": cmd_score, "replay": cmd_replay}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
