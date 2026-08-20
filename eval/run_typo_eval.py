"""Typo-checker eval runner: score the deterministic spelling/grammar pass against labels.

Runs typo_checker.check_typos(text, llm=None) — the same code path the live pipeline takes
before escalation — on each labelled case and compares what it flagged against what the case
says it should have flagged. No LLM, no API keys, fully reproducible.

Usage:
    python -m eval.run_typo_eval
    python -m eval.run_typo_eval --cases eval/cases/typo --json out.json --fail-under 1.0

--fail-under gates on PRECISION, not on the combined score: see eval/typo_dataset.py for why
recall on free-form misspellings is out of reach without an LLM, while a false accusation on a
correct word is both reachable and the more damaging failure.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from eval.typo_dataset import ExpectedIssue, TypoCase, load_typo_cases
from schemas import TypoIssue
from typo_checker import check_typos

_DEFAULT_TYPO_CASES_DIR = Path(__file__).parent / "cases" / "typo"


def _discover_case_files(cases_arg: Path) -> List[Path]:
    if cases_arg.is_dir():
        return sorted(cases_arg.glob("*.yaml")) + sorted(cases_arg.glob("*.yml"))
    return [cases_arg]


def _norm(word: str) -> str:
    """Compare labels to findings on wording alone: case and internal spacing are cosmetic.

    Reduplication findings carry two words ('Bank bank'), so whitespace is collapsed rather
    than stripped -- 'Bank  bank' and 'bank bank' are the same finding, 'bankbank' is not.
    """
    return re.sub(r"\s+", " ", word).strip().casefold()


@dataclass
class TypoCaseResult:
    id: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    missed: List[ExpectedIssue] = field(default_factory=list)
    spurious: List[TypoIssue] = field(default_factory=list)
    # Word flagged as labelled, but with a category or suggestion the case did not expect.
    detail_mismatches: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.missed and not self.spurious and not self.detail_mismatches


@dataclass
class TypoMetrics:
    cases: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def evaluate_case(case: TypoCase) -> TypoCaseResult:
    found = list(check_typos(case.text, llm=None).issues)
    result = TypoCaseResult(id=case.id)

    for expected in case.expect_flagged:
        match = next((i for i in found if _norm(i.word) == _norm(expected.word)), None)
        if match is None:
            result.false_negatives += 1
            result.missed.append(expected)
            continue
        found.remove(match)
        result.true_positives += 1
        if expected.category is not None and match.category != expected.category:
            result.detail_mismatches.append(
                f"{expected.word!r}: category {match.category!r}, expected {expected.category!r}"
            )
        if expected.suggestion is not None and _norm(match.suggestion) != _norm(expected.suggestion):
            result.detail_mismatches.append(
                f"{expected.word!r}: suggestion {match.suggestion!r}, expected {expected.suggestion!r}"
            )

    # Whatever is left was flagged without a label asking for it.
    result.false_positives = len(found)
    result.spurious = found
    return result


def run(cases: List[TypoCase]) -> Tuple[List[TypoCaseResult], TypoMetrics]:
    results = [evaluate_case(c) for c in cases]
    metrics = TypoMetrics(
        cases=len(results),
        true_positives=sum(r.true_positives for r in results),
        false_positives=sum(r.false_positives for r in results),
        false_negatives=sum(r.false_negatives for r in results),
    )
    return results, metrics


def render_console(metrics: TypoMetrics, results: List[TypoCaseResult]) -> str:
    lines: List[str] = []
    lines.append("=" * 62)
    lines.append("  TYPO CHECKER — DETERMINISTIC PASS EVAL (no LLM)")
    lines.append("=" * 62)
    lines.append("")
    lines.append(f"Cases          : {metrics.cases}")
    lines.append(
        f"Flags          : {metrics.true_positives} correct, "
        f"{metrics.false_positives} spurious, {metrics.false_negatives} missed"
    )
    lines.append(f"Precision      : {metrics.precision * 100:.1f}%   (of what it flagged, how much was real)")
    lines.append(f"Recall         : {metrics.recall * 100:.1f}%   (of the labelled issues, how many it caught)")
    lines.append(f"F1             : {metrics.f1 * 100:.1f}%")
    lines.append("")

    failures = [r for r in results if not r.passed]
    if not failures:
        lines.append("All cases passed. [OK]")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"FAILURES ({len(failures)}):")
    for r in failures:
        lines.append(f"  [FAIL] {r.id}")
        for s in r.spurious:
            lines.append(f"         spurious: {s.word!r} ({s.category}) -> {s.suggestion!r}")
        for m in r.missed:
            lines.append(f"         missed  : {m.word!r}")
        for d in r.detail_mismatches:
            lines.append(f"         detail  : {d}")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, metrics: TypoMetrics, results: List[TypoCaseResult]) -> None:
    import json

    payload = {
        "summary": {
            "cases": metrics.cases,
            "true_positives": metrics.true_positives,
            "false_positives": metrics.false_positives,
            "false_negatives": metrics.false_negatives,
            "precision": metrics.precision,
            "recall": metrics.recall,
            "f1": metrics.f1,
        },
        "cases": [
            {
                "id": r.id,
                "true_positives": r.true_positives,
                "false_positives": r.false_positives,
                "false_negatives": r.false_negatives,
                "spurious": [{"word": s.word, "category": s.category, "suggestion": s.suggestion} for s in r.spurious],
                "missed": [m.word for m in r.missed],
                "detail_mismatches": r.detail_mismatches,
                "passed": r.passed,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Typo-checker deterministic-pass accuracy eval.")
    parser.add_argument(
        "--cases", type=Path, default=_DEFAULT_TYPO_CASES_DIR,
        help="YAML file or directory of case files (default: eval/cases/typo).",
    )
    parser.add_argument("--json", type=Path, default=None, help="Write a machine-readable report here.")
    parser.add_argument(
        "--fail-under", type=float, default=None,
        help="Exit non-zero if PRECISION is below this fraction (e.g. 1.0 for a CI gate).",
    )
    args = parser.parse_args(argv)

    # The checker's suggestions contain non-ASCII; force UTF-8 so printing the report does not
    # crash on a legacy Windows console codepage (cp1252).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    case_files = _discover_case_files(args.cases)
    if not case_files:
        print(f"No case files found at {args.cases}", file=sys.stderr)
        return 2

    cases = load_typo_cases(case_files)
    results, metrics = run(cases)

    print(render_console(metrics, results))
    if args.json:
        write_json(args.json, metrics, results)
        print(f"Wrote JSON report to {args.json}")

    exit_code = 0
    if not all(r.passed for r in results):
        exit_code = 1
    if args.fail_under is not None and metrics.precision < args.fail_under:
        print(f"FAIL: precision {metrics.precision:.3f} < fail-under {args.fail_under}", file=sys.stderr)
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
