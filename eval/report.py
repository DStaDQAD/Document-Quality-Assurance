"""Console + JSON rendering of comparison-eval results (no external deps)."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from eval.metrics import EvalMetrics


@dataclass
class CaseResult:
    id: str
    operation: str
    expected_verdict: str
    predicted_verdict: str
    verdict_ok: bool
    value_ok: Optional[bool]  # None when the case did not assert a computed_value
    computed_value: Optional[float]
    reasoning: str

    @property
    def passed(self) -> bool:
        return self.verdict_ok and self.value_ok is not False


def render_console(metrics: EvalMetrics, results: List[CaseResult]) -> str:
    lines: List[str] = []
    lines.append("=" * 62)
    lines.append("  PAIRED VERIFIER — COMPARISON-ENGINE EVAL (Layer 1)")
    lines.append("=" * 62)
    lines.append("")
    lines.append(f"Cases        : {metrics.total}")
    lines.append(f"Verdict acc. : {metrics.correct}/{metrics.total} = {metrics.accuracy * 100:.1f}%")
    lines.append(f"Macro-F1     : {metrics.macro_f1 * 100:.1f}%")
    lines.append("")

    # Per-class table
    lines.append("Per-verdict metrics:")
    lines.append(f"  {'verdict':<14}{'prec':>7}{'recall':>8}{'f1':>7}{'support':>9}")
    for lbl, cm in metrics.per_class.items():
        lines.append(
            f"  {lbl:<14}{cm.precision * 100:6.1f}%{cm.recall * 100:7.1f}%"
            f"{cm.f1 * 100:6.1f}%{cm.support:>9}"
        )
    lines.append("")

    # Confusion matrix
    labels = list(metrics.per_class.keys())
    lines.append("Confusion matrix (rows = expected, cols = predicted):")
    header = " " * 16 + "".join(f"{l[:9]:>11}" for l in labels)
    lines.append(header)
    for e in labels:
        row = f"  {e[:13]:<14}" + "".join(f"{metrics.confusion[e][p]:>11}" for p in labels)
        lines.append(row)
    lines.append("")

    # Failures
    failures = [r for r in results if not r.passed]
    if failures:
        lines.append(f"FAILURES ({len(failures)}):")
        for r in failures:
            got = r.predicted_verdict
            if r.value_ok is False:
                got += f" (computed={r.computed_value})"
            lines.append(f"  [FAIL] {r.id} [{r.operation}]: expected {r.expected_verdict}, got {got}")
            lines.append(f"         {r.reasoning}")
    else:
        lines.append("All cases passed. [OK]")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, metrics: EvalMetrics, results: List[CaseResult]) -> None:
    payload = {
        "summary": {
            "total": metrics.total,
            "correct": metrics.correct,
            "accuracy": metrics.accuracy,
            "macro_f1": metrics.macro_f1,
        },
        "per_class": {
            lbl: {
                "precision": cm.precision,
                "recall": cm.recall,
                "f1": cm.f1,
                "support": cm.support,
                "tp": cm.tp,
                "fp": cm.fp,
                "fn": cm.fn,
            }
            for lbl, cm in metrics.per_class.items()
        },
        "confusion": metrics.confusion,
        "cases": [
            {
                "id": r.id,
                "operation": r.operation,
                "expected_verdict": r.expected_verdict,
                "predicted_verdict": r.predicted_verdict,
                "verdict_ok": r.verdict_ok,
                "value_ok": r.value_ok,
                "computed_value": r.computed_value,
                "passed": r.passed,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
