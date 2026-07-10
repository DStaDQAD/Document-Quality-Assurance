"""Layer-2 eval runner: score the FULL pipeline (LLM extraction + verdict) on labelled docs.

For each labelled document it runs exactly what the live endpoint runs — extract_narrative_text
then verify_paired — matches the returned facts to the expected claims, and reports extraction
recall, spurious-fact count, and verdict accuracy (over matched claims). Because it invokes real
providers it needs API keys configured in .env and is run on demand, not in CI.

Usage:
    python -m eval.run_e2e_eval                                  # runs eval/cases/e2e/*.yaml
    python -m eval.run_e2e_eval --cases eval/cases/e2e/mycase.yaml --json out.json
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from eval.e2e_dataset import E2ECase, ExpectedClaim, load_e2e_cases
from eval.matching import MatchResult, match_results
from eval.metrics import compute_metrics
from eval.report import render_e2e_console
from schemas import FactVerificationResult

_DEFAULT_CASES_DIR = Path(__file__).parent / "cases" / "e2e"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _discover_case_files(cases_arg: Path) -> List[Path]:
    if cases_arg.is_dir():
        return sorted(cases_arg.glob("*.yaml")) + sorted(cases_arg.glob("*.yml"))
    return [cases_arg]


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else _REPO_ROOT / p


async def _run_case(case: E2ECase, llm, vision_llm) -> MatchResult:
    # Imported here so `import eval.run_e2e_eval` (e.g. from tests) doesn't pull in the pipeline.
    from paired_verifier import verify_paired
    from pdf_extraction import extract_narrative_text

    pdf_bytes = _resolve(case.pdf).read_bytes()
    excel_sources: List[Tuple[bytes, str, str]] = []
    for i, xls in enumerate(case.excel):
        p = _resolve(xls)
        excel_sources.append((p.read_bytes(), case.sheet_for(i), p.name))

    narrative = await extract_narrative_text(pdf_bytes, vision_llm)
    response = await verify_paired(
        narrative_text=narrative,
        excel_sources=excel_sources,
        llm=llm,
        pdf_filename=_resolve(case.pdf).name,
        vision_llm=vision_llm,
    )
    return match_results(case.claims, response.results)


async def _run_all(cases: List[E2ECase], llm, vision_llm):
    all_matched: List[Tuple[ExpectedClaim, FactVerificationResult]] = []
    all_missing: List[ExpectedClaim] = []
    all_spurious: List[FactVerificationResult] = []
    for case in cases:
        print(f"[e2e] running {case.id} ({case.pdf}) ...", file=sys.stderr)
        mr = await _run_case(case, llm, vision_llm)
        all_matched.extend(mr.matched)
        all_missing.extend(mr.missing)
        all_spurious.extend(mr.spurious)
    return all_matched, all_missing, all_spurious


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Layer-2 end-to-end accuracy eval (needs API keys).")
    parser.add_argument("--cases", type=Path, default=_DEFAULT_CASES_DIR)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    case_files = _discover_case_files(args.cases)
    if not case_files:
        print(f"No e2e case files found at {args.cases}", file=sys.stderr)
        return 2
    cases = load_e2e_cases(case_files)
    if not any(c.claims for c in cases):
        print("Loaded cases but none have labelled claims yet — fill in eval/cases/e2e/*.yaml.",
              file=sys.stderr)
        return 2

    # Providers are validated at import; surface a clear message instead of a traceback.
    try:
        from llm_provider import get_llm, get_vision_llm
    except RuntimeError as exc:
        print(f"LLM provider not configured: {exc}", file=sys.stderr)
        print("Layer 2 needs real API keys in .env (LLM_PROVIDER + the matching *_API_KEY).",
              file=sys.stderr)
        return 2

    llm = get_llm(temperature=0.0)
    try:
        vision_llm = get_vision_llm()
    except RuntimeError:
        vision_llm = None
        print("Vision LLM unavailable; PDFs without a text layer may extract poorly.", file=sys.stderr)

    matched, missing, spurious = asyncio.run(_run_all(cases, llm, vision_llm))
    verdict_metrics = compute_metrics([(c.expected_verdict, r.verdict) for c, r in matched])

    print(render_e2e_console(matched, missing, spurious, verdict_metrics))
    if args.json:
        _write_json(args.json, matched, missing, spurious, verdict_metrics)
        print(f"Wrote JSON report to {args.json}")
    return 0


def _write_json(path: Path, matched, missing, spurious, verdict_metrics) -> None:
    import json

    total_expected = len(matched) + len(missing)
    payload = {
        "summary": {
            "expected_claims": total_expected,
            "matched": len(matched),
            "missing": len(missing),
            "spurious": len(spurious),
            "extraction_recall": len(matched) / total_expected if total_expected else 0.0,
            "verdict_accuracy": verdict_metrics.accuracy,
            "verdict_macro_f1": verdict_metrics.macro_f1,
        },
        "verdict_confusion": verdict_metrics.confusion,
        "matched": [
            {
                "metric": c.metric,
                "operation": c.operation,
                "expected_verdict": c.expected_verdict,
                "predicted_verdict": r.verdict,
                "computed_value": r.computed_value,
                "page_number": r.page_number,
            }
            for c, r in matched
        ],
        "missing": [{"metric": c.metric, "operation": c.operation, "expected_verdict": c.expected_verdict} for c in missing],
        "spurious": [{"metric_label": r.metric_label, "operation": r.operation, "verdict": r.verdict} for r in spurious],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
