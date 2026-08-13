"""Deterministic tests for the Layer-2 eval's matching + loader (no LLM, no API keys)."""

from dataclasses import replace
from pathlib import Path

import pytest

from eval.dataset import PeriodSpec
from eval.e2e_dataset import ExpectedClaim, load_e2e_cases
from eval.matching import match_results
from eval.report import render_e2e_console
from eval.run_e2e_eval import _DEFAULT_CASES_DIR, _discover_case_files
from eval.metrics import compute_metrics
from schemas import FactVerificationResult, PeriodResult


def _result(operation, metric_label, periods, verdict="Entailed"):
    return FactVerificationResult(
        operation=operation,
        metric_label=metric_label,
        periods=[PeriodResult(metric_label=metric_label, year=y, month=m) for (y, m) in periods],
        verdict=verdict,
        reasoning="",
        context_quote="q",
    )


def _claim(operation, metric, periods, verdict="Entailed"):
    return ExpectedClaim(
        metric=metric,
        operation=operation,
        periods=[PeriodSpec(metric_label=metric, year=y, month=m) for (y, m) in periods],
        expected_verdict=verdict,
    )


def test_match_results_matched_missing_and_spurious():
    claims = [
        _claim("value", "M2", [(2026, "Apr")]),
        _claim("yoy_growth", "M2", [(2026, "Apr")]),
        _claim("value", "M2", [(2026, "May")]),  # not extracted → missing
    ]
    results = [
        _result("value", "Uang Beredar Luas(M2)", [(2026, "Apr")]),
        # yoy result carries the auto-fetched prior-year point the label doesn't list:
        _result("yoy_growth", "M2", [(2026, "Apr"), (2025, "Apr")]),
        _result("value", "M1", [(2026, "Apr")]),  # unlabelled → spurious
    ]

    mr = match_results(claims, results)

    assert len(mr.matched) == 2
    assert {c.operation for c, _ in mr.matched} == {"value", "yoy_growth"}
    # metric containment ("M2" ⊂ "Uang Beredar Luas(M2)") and yoy period-subset both worked
    assert mr.matched[0][1].metric_label == "Uang Beredar Luas(M2)"
    assert len(mr.missing) == 1
    assert mr.missing[0].periods[0].month == "May"
    assert len(mr.spurious) == 1
    assert mr.spurious[0].metric_label == "M1"


def test_match_results_disambiguates_metrics_sharing_periods():
    claims = [_claim("value", "M1", [(2026, "Apr")]), _claim("value", "M2", [(2026, "Apr")])]
    results = [
        _result("value", "Uang Beredar Luas(M2)", [(2026, "Apr")]),
        _result("value", "M1", [(2026, "Apr")]),
    ]

    mr = match_results(claims, results)

    assert not mr.missing and not mr.spurious
    by_metric = {c.metric: r.metric_label for c, r in mr.matched}
    assert by_metric == {"M1": "M1", "M2": "Uang Beredar Luas(M2)"}


def test_match_results_operation_mismatch_is_not_matched():
    claims = [_claim("yoy_growth", "M2", [(2026, "Apr")])]
    results = [_result("value", "M2", [(2026, "Apr")])]

    mr = match_results(claims, results)

    assert not mr.matched
    assert len(mr.missing) == 1
    assert len(mr.spurious) == 1


def test_render_e2e_console_reports_recall_and_verdicts():
    claims = [_claim("value", "M2", [(2026, "Apr")], verdict="Entailed")]
    results = [_result("value", "M2", [(2026, "Apr")], verdict="Refuted")]
    mr = match_results(claims, results)
    metrics = compute_metrics([(c.expected_verdict, r.verdict) for c, r in mr.matched])

    # The runner tags every row with its case id before rendering (see _run_all).
    text = render_e2e_console(
        [("m2_internal", c, r) for c, r in mr.matched],
        [("m2_internal", c) for c in mr.missing],
        [("m2_internal", r) for r in mr.spurious],
        metrics,
    )

    assert "END-TO-END EVAL" in text
    assert "Extraction recall" in text
    assert "WRONG verdicts" in text  # Entailed expected but Refuted predicted
    assert "m2_internal" in text, "a wrong verdict must name the document it came from"


def test_example_e2e_case_loads_and_is_well_formed():
    files = _discover_case_files(_DEFAULT_CASES_DIR)
    assert files, "no e2e case files found"
    cases = load_e2e_cases(files)

    example = next(c for c in cases if c.id == "m2_april_2026_narrative")
    assert example.claims, "example should ship with at least one labelled claim"
    assert {c.operation for c in example.claims} == {"value", "yoy_growth"}
    assert all(c.expected_verdict in {"Entailed", "Refuted", "Inconclusive"} for c in example.claims)
    assert example.sheet_for(0) == "I.1"
    assert example.sheet_for(5) == "I.1"  # reused when fewer sheets than files
    assert example.mode == "excel", "a case that says nothing about mode is an Excel case"


# ---------------------------------------------------------------------------
# mode: which reference pool a case is scored against
# ---------------------------------------------------------------------------

def _case_yaml(tmp_path, body):
    path = tmp_path / "case.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_INTERNAL_CASE = """
- id: internal_case
  mode: internal
  pdf: "sample_data/report.pdf"
  claims:
    - metric: "M2"
      operation: value
      periods:
        - {metric_label: "M2", year: 2026, month: Apr}
      expected_verdict: Entailed
"""


def test_internal_case_needs_no_excel_source(tmp_path):
    case = load_e2e_cases([_case_yaml(tmp_path, _INTERNAL_CASE)])[0]
    assert case.mode == "internal"
    assert case.excel == []


def test_unknown_mode_is_rejected_at_load_time(tmp_path):
    body = _INTERNAL_CASE.replace("mode: internal", "mode: telepathy")
    with pytest.raises(ValueError, match="telepathy"):
        load_e2e_cases([_case_yaml(tmp_path, body)])


def test_a_mode_that_needs_a_workbook_is_rejected_without_one(tmp_path):
    body = _INTERNAL_CASE.replace("mode: internal", "mode: both")
    with pytest.raises(ValueError, match="internal_case"):
        load_e2e_cases([_case_yaml(tmp_path, body)])


def test_internal_mode_cases_ship_with_a_refuted_label():
    # The doctored M2 report is the only case that proves the tool catches a wrong number
    # from the document alone; a file that lost it would still pass every other assertion.
    cases = load_e2e_cases(_discover_case_files(_DEFAULT_CASES_DIR))
    internal = [c for c in cases if c.mode == "internal"]
    assert internal, "no internal-mode case is labelled"
    verdicts = {claim.expected_verdict for case in internal for claim in case.claims}
    assert "Refuted" in verdicts


# ---------------------------------------------------------------------------
# claimed_value tie-break — the same claim stated twice with different numbers
# ---------------------------------------------------------------------------

def test_claimed_value_picks_the_sentence_a_label_is_about():
    # A doctored summary bullet (8,7) and an untouched body paragraph (9,7) extract to two
    # facts that agree on metric, operation and period. Order must not decide the scoring.
    claim = _claim("yoy_growth", "M2", [(2026, "Mar")], verdict="Refuted")
    claim = replace(claim, claimed_value=8.7)
    correct = _result("yoy_growth", "M2", [(2026, "Mar")], verdict="Entailed")
    correct.claimed_value = 9.7
    doctored = _result("yoy_growth", "M2", [(2026, "Mar")], verdict="Refuted")
    doctored.claimed_value = 8.7

    mr = match_results([claim], [correct, doctored])

    assert mr.matched[0][1] is doctored
    assert mr.matched[0][1].verdict == "Refuted"


def test_a_label_without_claimed_value_still_takes_the_first_fit():
    claim = _claim("value", "M2", [(2026, "Apr")])
    first = _result("value", "M2", [(2026, "Apr")])
    first.claimed_value = 10253.7
    second = _result("value", "M2", [(2026, "Apr")])

    mr = match_results([claim], [first, second])

    assert mr.matched[0][1] is first
