"""Tests for the typo-checker accuracy harness (eval/typo_dataset.py, eval/run_typo_eval.py)."""

from pathlib import Path

import pytest

from eval.run_typo_eval import _DEFAULT_TYPO_CASES_DIR, _discover_case_files, evaluate_case, run
from eval.typo_dataset import load_typo_cases


def _write_cases(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "typo_cases.yaml"
    path.write_text(text, encoding="utf-8")
    return path


_ONE_CASE = """
- id: probe_tidak_baku
  description: "A curated non-standard word the deterministic pass must catch"
  text: "Analisa tersebut menunjukkan pertumbuhan kredit yang stabil."
  expect_flagged:
    - {word: "Analisa", category: tidak_baku, suggestion: "analisis"}
  expect_clean: ["pertumbuhan", "kredit"]
"""


def test_loader_reads_the_text_and_both_label_sides(tmp_path):
    case = load_typo_cases([_write_cases(tmp_path, _ONE_CASE)])[0]

    assert case.id == "probe_tidak_baku"
    assert case.text.startswith("Analisa tersebut")
    assert [e.word for e in case.expect_flagged] == ["Analisa"]
    assert case.expect_clean == ["pertumbuhan", "kredit"]


def test_a_flagged_expected_word_counts_as_a_true_positive(tmp_path):
    case = load_typo_cases([_write_cases(tmp_path, _ONE_CASE)])[0]
    result = evaluate_case(case)

    assert result.true_positives == 1
    assert result.missed == []
    assert result.spurious == []
    assert result.passed


def test_an_unexpected_flag_counts_as_a_false_positive(tmp_path):
    # The word IS flagged by the checker (it is on the curated tidak-baku list), but the case
    # does not expect it -- exactly the shape a regression on real report vocabulary takes.
    unlabelled = """
- id: probe_unlabelled_flag
  description: "A real flag the case deliberately does not expect"
  text: "Analisa tersebut menunjukkan pertumbuhan kredit yang stabil."
  expect_flagged: []
"""
    case = load_typo_cases([_write_cases(tmp_path, unlabelled)])[0]
    result = evaluate_case(case)

    assert result.false_positives == 1
    assert [s.word for s in result.spurious] == ["Analisa"]
    assert not result.passed


def test_an_expected_word_the_checker_misses_counts_as_a_false_negative(tmp_path):
    # 'Likuidiaftas' is unknown to the id_ID dictionary, so the deterministic pass escalates
    # it and -- with no LLM -- drops it. The harness must record that as a miss, not a pass.
    missed = """
- id: probe_missed_spelling
  description: "A plain misspelling the deterministic pass cannot reach"
  text: "Likuidiaftas perekonomian pada Mei 2026 tumbuh lebih tinggi."
  expect_flagged:
    - {word: "Likuidiaftas", category: ejaan}
"""
    case = load_typo_cases([_write_cases(tmp_path, missed)])[0]
    result = evaluate_case(case)

    assert result.false_negatives == 1
    assert [m.word for m in result.missed] == ["Likuidiaftas"]
    assert not result.passed


def test_run_aggregates_precision_and_recall_over_cases(tmp_path):
    case = load_typo_cases([_write_cases(tmp_path, _ONE_CASE)])[0]
    _results, metrics = run([case])

    assert metrics.true_positives == 1
    assert metrics.precision == pytest.approx(1.0)
    assert metrics.recall == pytest.approx(1.0)
    assert metrics.f1 == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# The seed dataset must stay clean (regression guard, same role as Layer 1's)
# ---------------------------------------------------------------------------

def test_seed_typo_dataset_all_pass():
    case_files = _discover_case_files(_DEFAULT_TYPO_CASES_DIR)
    assert case_files, "no seed typo cases found"

    cases = load_typo_cases(case_files)
    results, metrics = run(cases)

    failures = [r for r in results if not r.passed]
    assert not failures, "seed typo cases regressed:\n" + "\n".join(
        f"  {r.id}: missed={[m.word for m in r.missed]} "
        f"spurious={[s.word for s in r.spurious]} detail={r.detail_mismatches}"
        for r in failures
    )
    assert metrics.precision == 1.0
