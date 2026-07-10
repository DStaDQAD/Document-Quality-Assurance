"""Tests for the Layer-1 accuracy eval harness (eval/)."""

from pathlib import Path

import pytest

from eval.dataset import load_cases
from eval.metrics import compute_metrics
from eval.run_comparison_eval import _DEFAULT_CASES_DIR, _discover_case_files, run


# ---------------------------------------------------------------------------
# metrics.compute_metrics
# ---------------------------------------------------------------------------

def test_compute_metrics_confusion_and_per_class():
    pairs = [
        ("Entailed", "Entailed"),
        ("Entailed", "Refuted"),
        ("Refuted", "Refuted"),
        ("Inconclusive", "Inconclusive"),
        ("Refuted", "Inconclusive"),
    ]
    m = compute_metrics(pairs)

    assert m.total == 5
    assert m.correct == 3
    assert m.accuracy == pytest.approx(0.6)

    # Confusion matrix (expected -> predicted)
    assert m.confusion["Entailed"] == {"Entailed": 1, "Refuted": 1, "Inconclusive": 0}
    assert m.confusion["Refuted"] == {"Entailed": 0, "Refuted": 1, "Inconclusive": 1}
    assert m.confusion["Inconclusive"] == {"Entailed": 0, "Refuted": 0, "Inconclusive": 1}

    ent = m.per_class["Entailed"]
    assert (ent.tp, ent.fp, ent.fn, ent.support) == (1, 0, 1, 2)
    assert ent.precision == pytest.approx(1.0)
    assert ent.recall == pytest.approx(0.5)

    ref = m.per_class["Refuted"]
    assert (ref.tp, ref.fp, ref.fn) == (1, 1, 1)
    assert ref.precision == pytest.approx(0.5)
    assert ref.recall == pytest.approx(0.5)

    assert m.macro_f1 == pytest.approx((2 / 3 + 0.5 + 2 / 3) / 3)


def test_compute_metrics_handles_zero_division_and_unknown_labels():
    # An unseen verdict string must surface in the report, not be dropped.
    m = compute_metrics([("Entailed", "Bogus")])
    assert "Bogus" in m.per_class
    assert m.per_class["Entailed"].recall == 0.0  # expected Entailed, never predicted it
    assert m.per_class["Bogus"].precision == 0.0  # predicted Bogus, never correct
    assert m.accuracy == 0.0


# ---------------------------------------------------------------------------
# End-to-end: the seeded comparison dataset must stay 100% (regression guard)
# ---------------------------------------------------------------------------

def test_seed_comparison_dataset_all_pass():
    case_files = _discover_case_files(_DEFAULT_CASES_DIR)
    assert case_files, "no seed comparison cases found"

    cases = load_cases(case_files)
    results, metrics = run(cases)

    failures = [r for r in results if not r.passed]
    assert not failures, "seed comparison cases regressed:\n" + "\n".join(
        f"  {r.id}: expected {r.expected_verdict}, got {r.predicted_verdict} ({r.reasoning})"
        for r in failures
    )
    assert metrics.accuracy == 1.0


def test_every_operation_is_covered_by_the_seed_dataset():
    cases = load_cases(_discover_case_files(_DEFAULT_CASES_DIR))
    ops = {c.fact.operation for c in cases}
    expected_ops = {
        "value", "yoy_growth", "average", "sum", "diff", "ratio",
        "is_increasing", "is_decreasing", "is_stable",
    }
    assert expected_ops <= ops, f"missing coverage for: {expected_ops - ops}"


def test_all_three_verdicts_are_represented():
    cases = load_cases(_discover_case_files(_DEFAULT_CASES_DIR))
    verdicts = {c.expected.verdict for c in cases}
    assert verdicts == {"Entailed", "Refuted", "Inconclusive"}
