"""Regression run over a corpus of real documents, compared claim by claim.

    python -m eval.run_regression run   --manifest M.yaml [--manifest M2.yaml] --out DIR
                                        [--only id1,id2] [--data-root PATH] [--no-ai-review]
    python -m eval.run_regression diff  BASE_DIR NEW_DIR [--all]
    python -m eval.run_regression score DIR --manifest M.yaml [...]

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

from eval.regression import Document, diff_runs, load_manifest, score_label, summarize

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
    for doc in docs:
        target = out / f"{doc.id}.json"
        if target.exists():
            print(f"[{doc.id}] sudah ada, dilewati")
            continue
        files, sheets = _request_files(doc, data_root)
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
    p = sub.add_parser("score")
    p.add_argument("dir")
    p.add_argument("--manifest", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    return {"run": cmd_run, "diff": cmd_diff, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
