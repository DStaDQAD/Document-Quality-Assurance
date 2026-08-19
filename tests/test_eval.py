"""Tests for the Layer-1 accuracy eval harness (eval/)."""

from pathlib import Path
from typing import get_args

import pytest

from eval.dataset import build_fact, build_sources, load_cases
from eval.metrics import compute_metrics
from eval.run_comparison_eval import _DEFAULT_CASES_DIR, _discover_case_files, evaluate_case, run
from schemas import FactVerificationResult


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


def test_every_operation_the_engine_can_return_is_covered_by_the_seed_dataset():
    # Derived from the schema, not a hand-kept list: the previous version enumerated nine
    # operations and passed while the engine could return eleven, so the two threshold ops
    # went unmeasured without anything saying so.
    engine_ops = set(get_args(FactVerificationResult.model_fields["operation"].annotation))
    cases = load_cases(_discover_case_files(_DEFAULT_CASES_DIR))
    ops = {c.fact.operation for c in cases}
    assert engine_ops <= ops, f"missing coverage for: {sorted(engine_ops - ops)}"


def test_all_three_verdicts_are_represented():
    cases = load_cases(_discover_case_files(_DEFAULT_CASES_DIR))
    verdicts = {c.expected.verdict for c in cases}
    assert verdicts == {"Entailed", "Refuted", "Inconclusive"}


# ---------------------------------------------------------------------------
# Loader: categorical (non-time-series) cases
# ---------------------------------------------------------------------------

_CATEGORICAL_YAML = """
- id: cat_loader_probe
  description: "Attribute claim against an item list"
  table:
    title: "Daftar Harga Barang"
    filename: "penjualan_bertingkat.xlsx"
    sheet: "Sheet1"
    data:
      - {label: "Beras Premium", col_label: "Harga (Rp)", value: 15000}
      - {label: "Beras Premium", col_label: "Stok", value: 120}
      - {label: "Minyak Goreng", col_label: "Harga (Rp)", value: 18500}
  fact:
    operation: value
    claimed_value: 15000
    unit: "Rp"
    periods:
      - {metric_label: "Beras Premium", col_label: "Harga (Rp)"}
  expected:
    verdict: Entailed
"""


def _write_cases(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cases.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loader_builds_a_categorical_source_from_col_label_cells(tmp_path):
    case = load_cases([_write_cases(tmp_path, _CATEGORICAL_YAML)])[0]
    source = build_sources(case)[0]

    assert source.table.axis_type == "categorical"
    assert source.table.row_labels == ["Beras Premium", "Minyak Goreng"]
    assert source.table.col_labels == ["Harga (Rp)", "Stok"]
    assert source.table.lookup_cell("Beras Premium", "Harga (Rp)") == 15000


def test_loader_builds_a_categorical_fact_point(tmp_path):
    case = load_cases([_write_cases(tmp_path, _CATEGORICAL_YAML)])[0]
    point = build_fact(case).periods[0]

    assert point.col_label == "Harga (Rp)"
    assert point.year is None and point.month is None


def test_loader_rejects_a_table_mixing_temporal_and_categorical_cells(tmp_path):
    mixed = """
- id: cat_loader_mixed
  table:
    title: "Campuran"
    data:
      - {label: "Beras Premium", col_label: "Harga (Rp)", value: 15000}
      - {label: "M2", year: 2026, month: Apr, value: 10253651.888}
  fact:
    operation: value
    claimed_value: 15000
    periods:
      - {metric_label: "Beras Premium", col_label: "Harga (Rp)"}
  expected:
    verdict: Entailed
"""
    with pytest.raises(ValueError, match="mixes"):
        load_cases([_write_cases(tmp_path, mixed)])


def test_loader_rejects_a_temporal_cell_missing_its_period(tmp_path):
    # Without year+month the cell would land under a (label, None, None) key that no
    # lookup can ever reach — the case would silently score the engine on nothing.
    incomplete = """
- id: cat_loader_incomplete
  table:
    title: "Tanpa periode"
    data:
      - {label: "M2", value: 10253651.888}
  fact:
    operation: value
    claimed_value: 10253651.9
    periods:
      - {metric_label: "M2", year: 2026, month: Apr}
  expected:
    verdict: Entailed
"""
    with pytest.raises(ValueError, match="year"):
        load_cases([_write_cases(tmp_path, incomplete)])


# ---------------------------------------------------------------------------
# Loader + runner: several reference sources per case (source_conflict)
# ---------------------------------------------------------------------------

_TWO_SOURCES_YAML = """
- id: multi_loader_probe
  description: "Same series carried by two sources"
  tables:
    - title: "Uang Beredar dan faktor-faktor yang mempengaruhinya"
      unit: "Miliar Rp"
      filename: "TABEL1_1.xls"
      sheet: "I.1"
      data:
        - {label: "Uang Beredar Luas(M2)", year: 2026, month: Apr, value: 10253651.888}
    - title: "Uang Beredar dan faktor-faktor yang mempengaruhinya"
      unit: "Miliar Rp"
      filename: "M2-April-2026.pdf"
      sheet: "Lampiran 1"
      origin: pdf
      data:
        - {label: "Uang Beredar Luas(M2)", year: 2026, month: Apr, value: %s}
  fact:
    operation: value
    unit: "triliun Rp"
    claimed_value: 10253.7
    context_quote: "M2 pada April 2026 tercatat sebesar Rp10.253,7 triliun"
    periods:
      - {metric_label: "Uang Beredar Luas(M2)", year: 2026, month: Apr}
  expected:
    verdict: Entailed
%s
"""


def _two_sources(tmp_path: Path, second_value: str, expected_extra: str = "") -> Path:
    return _write_cases(tmp_path, _TWO_SOURCES_YAML % (second_value, expected_extra))


def test_loader_builds_one_source_per_table_entry(tmp_path):
    case = load_cases([_two_sources(tmp_path, "10253651.888")])[0]
    sources = build_sources(case)

    assert [s.label for s in sources] == ["TABEL1_1.xls / I.1", "M2-April-2026.pdf / Lampiran 1"]
    assert [s.origin for s in sources] == ["excel", "pdf"]


def test_evaluate_case_scores_an_expected_source_conflict(tmp_path):
    # The two sources disagree by far more than MATCH_TOLERANCE, so the engine must flag
    # a cross-pool conflict while still answering from the source it matched best.
    case = load_cases([_two_sources(tmp_path, "10250000.0", "    source_conflict: cross")])[0]
    result = evaluate_case(case)

    assert result.predicted_verdict == "Entailed"
    assert result.conflict_ok is True
    assert result.passed


def test_evaluate_case_fails_when_an_unlabelled_case_hits_a_conflict(tmp_path):
    # No source_conflict in `expected` means "the sources agree" — a case that silently
    # starts conflicting must fail rather than pass on its verdict alone.
    case = load_cases([_two_sources(tmp_path, "10250000.0")])[0]
    result = evaluate_case(case)

    assert result.conflict_ok is False
    assert not result.passed
