"""Scoring helpers of the Tinjauan AI eval runner (the LLM part is never run in tests)."""

from eval.run_ai_review_eval import claim_id, cost_rupiah, score, stability


def _row(id_, before, outcome):
    after = {"to_entailed": "Entailed", "to_inconclusive": "Inconclusive",
             "to_refuted": "Refuted"}.get(outcome, before)
    return {"id": id_, "pub": "npi", "before": before, "after": after, "outcome": outcome,
            "quote": "q", "note": ""}


def test_claim_id_is_stable_and_depends_on_the_claim():
    r = {"context_quote": "Impor turun.", "operation": "value", "claimed_value": 9.0, "metric_label": "Impor"}
    assert claim_id("npi", r) == claim_id("npi", dict(r))
    assert claim_id("npi", r) != claim_id("npi", {**r, "claimed_value": 9.1})
    assert claim_id("npi", r).startswith("npi:")


def test_score_counts_precision_per_outcome_over_labelled_claims_only():
    rows = [_row("a", "Refuted", "to_entailed"), _row("b", "Inconclusive", "to_entailed"),
            _row("c", "Refuted", "to_inconclusive"), _row("d", "Inconclusive", "flag"),
            _row("e", "Inconclusive", "unchanged")]
    labels = {"a": {"truth": "Entailed"}, "b": {"truth": "Refuted"}, "c": {"truth": "Refuted"},
              "d": {"truth": "Refuted"}}
    s = score(rows, labels)
    assert s["to_entailed"] == {"n": 2, "labelled": 2, "correct": 1, "precision": 0.5}
    assert s["to_inconclusive"]["precision"] == 0.0
    assert s["flag"]["precision"] == 1.0
    assert s["to_refuted"]["precision"] is None
    assert s["lost_findings"] == 1          # c: a true Tidak Sesuai turned Tidak Cukup Data
    assert s["unlabelled"] == []


def test_score_lists_changed_claims_still_missing_a_label():
    s = score([_row("x", "Inconclusive", "to_entailed"), _row("y", "Refuted", "unchanged")], {})
    assert s["unlabelled"] == ["x"]


def test_stability_compares_outcomes_overall_and_among_changed_claims():
    a = [_row("1", "Refuted", "to_entailed"), _row("2", "Refuted", "unchanged"), _row("3", "Inconclusive", "flag")]
    b = [_row("1", "Refuted", "to_entailed"), _row("2", "Refuted", "unchanged"), _row("3", "Inconclusive", "unchanged")]
    assert stability(a, b) == (2, 3, 1, 2)


def test_cost_uses_gemini_flash_prices_in_rupiah():
    assert round(cost_rupiah(1_000_000, 0)) == 4950      # $0.30
    assert round(cost_rupiah(0, 1_000_000)) == 41250     # $2.50
