"""Deterministic tests for the regression runner's manifest, label scoring and per-claim diff.

No LLM and no documents: results are the dicts /api/verify-paired returns, built by hand.
"""

from pathlib import Path

import pytest

from eval.regression import diff_runs, load_manifest, score_label, Label


def _r(quote, verdict, operation="is_increasing", value=None, metric="Penjualan", reasoning=""):
    return {"context_quote": quote, "verdict": verdict, "operation": operation,
            "claimed_value": value, "metric_label": metric, "reasoning": reasoning}


# --- manifest ---------------------------------------------------------------------------------

def test_load_manifest_reads_documents_sources_and_labels(tmp_path: Path):
    m = tmp_path / "m.yaml"
    m.write_text(
        "- id: toko-mei\n"
        "  publication: sk\n"
        "  doc: data/laporan.docx\n"
        "  sources:\n"
        "    - {file: data/tabel.xlsx, sheets: ['Tabel 1', 'Tabel 2']}\n"
        "    - {file: data/semua.xlsx}\n"
        "  labels:\n"
        "    - {quote: 'naik di seluruh toko', expected: Entailed, note: 'cek manual'}\n",
        encoding="utf-8",
    )
    [doc] = load_manifest([m])
    assert doc.id == "toko-mei" and doc.publication == "sk" and doc.mode == "excel"
    assert doc.sources[0].sheets == ["Tabel 1", "Tabel 2"]
    assert doc.sources[1].sheets == "*"          # no list = every sheet, like ticking them all
    assert doc.labels == [Label(quote="naik di seluruh toko", expected="Entailed", note="cek manual")]


def test_load_manifest_rejects_an_unknown_expected_verdict(tmp_path: Path):
    m = tmp_path / "m.yaml"
    m.write_text("- {id: a, publication: sk, doc: x.docx, sources: [{file: t.xlsx}],"
                 " labels: [{quote: q, expected: Benar}]}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Benar"):
        load_manifest([m])


def test_load_manifest_rejects_duplicate_ids(tmp_path: Path):
    m = tmp_path / "m.yaml"
    m.write_text("- {id: a, publication: sk, doc: x.docx, sources: [{file: t.xlsx}]}\n"
                 "- {id: a, publication: sk, doc: y.docx, sources: [{file: t.xlsx}]}\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="a"):
        load_manifest([m])


# --- scoring a label --------------------------------------------------------------------------

def test_score_label_is_missing_when_no_result_quotes_the_sentence():
    label = Label(quote="toko C turun", expected="Entailed")
    assert score_label(label, [_r("Penjualan toko A naik.", "Entailed")]).status == "hilang"


def test_score_label_matches_the_quote_ignoring_case_and_spacing():
    label = Label(quote="Toko A  naik", expected="Entailed")
    assert score_label(label, [_r("Penjualan toko a naik.", "Entailed")]).status == "benar"


def test_a_true_claim_split_into_several_facts_is_right_only_when_every_fact_is():
    """A multi-group sentence may come back as one fact per group. Calling the claim right
    when one of its parts is still Tidak Cukup Data would hide exactly the gap being fixed."""
    label = Label(quote="naik di seluruh toko", expected="Entailed")
    results = [_r("Penjualan naik di seluruh toko.", "Entailed", metric="toko A"),
               _r("Penjualan naik di seluruh toko.", "Inconclusive", metric="toko B")]
    assert score_label(label, results).status == "salah"


def test_a_seeded_error_counts_as_caught_when_any_part_is_refuted():
    """'naik di seluruh toko' with one shop falling: the shops that did rise are rightly
    Sesuai; the claim is caught as soon as the shop that fell is Tidak Sesuai."""
    label = Label(quote="naik di seluruh toko", expected="Refuted")
    results = [_r("Penjualan naik di seluruh toko.", "Entailed", metric="toko A"),
               _r("Penjualan naik di seluruh toko.", "Refuted", metric="toko B")]
    assert score_label(label, results).status == "benar"


def test_score_label_narrows_by_operation_and_value_when_given():
    label = Label(quote="toko A", expected="Entailed", operation="yoy_growth", claimed_value=-0.3)
    results = [_r("Penjualan toko A turun 0,3% (yoy) menjadi 12,5.", "Inconclusive",
                  operation="value", value=12.5),
               _r("Penjualan toko A turun 0,3% (yoy) menjadi 12,5.", "Entailed",
                  operation="yoy_growth", value=-0.3)]
    assert score_label(label, results).status == "benar"


# --- per-claim diff ---------------------------------------------------------------------------

def test_diff_pairs_the_same_claim_despite_a_different_metric_label():
    """The extractor names the same row differently from run to run ('Jasa / TOTAL' vs
    'Jasa / TOTAL > Pemerintah'); the quote, operation and number identify the claim."""
    base = [_r("Pangsa jasa 16,2%.", "Entailed", "ratio", 16.2, "Jasa / TOTAL")]
    new = [_r("Pangsa jasa 16,2%.", "Entailed", "ratio", 16.2, "Jasa / TOTAL > Pemerintah")]
    [change] = diff_runs(base, new)
    assert change.kind == "sama"


def test_diff_reports_a_changed_verdict_and_flags_a_lost_entailed():
    base = [_r("Toko A naik.", "Entailed")]
    new = [_r("Toko A naik.", "Inconclusive")]
    [change] = diff_runs(base, new)
    assert change.kind == "berubah" and change.needs_review


def test_diff_does_not_flag_inconclusive_becoming_entailed():
    [change] = diff_runs([_r("Toko A naik.", "Inconclusive")], [_r("Toko A naik.", "Entailed")])
    assert change.kind == "berubah" and not change.needs_review


def test_diff_flags_a_new_refuted_claim_and_a_vanished_entailed_one():
    base = [_r("Toko A naik.", "Entailed")]
    new = [_r("Toko B turun 5,0%.", "Refuted", "yoy_growth", -5.0)]
    changes = {c.kind: c for c in diff_runs(base, new)}
    assert changes["hilang"].needs_review          # a Sesuai claim no longer checked at all
    assert changes["baru"].needs_review            # a new Tidak Sesuai must be explained


def test_diff_pairs_duplicate_keys_by_metric_label():
    """One sentence, one operation, no number: 'naik di toko A dan toko B' gives two facts that
    differ only in their metric. Each must be compared with its own counterpart."""
    base = [_r("Naik di toko A dan B.", "Entailed", metric="toko A"),
            _r("Naik di toko A dan B.", "Inconclusive", metric="toko B")]
    new = [_r("Naik di toko A dan B.", "Inconclusive", metric="toko B"),
           _r("Naik di toko A dan B.", "Entailed", metric="toko A")]
    assert [c.kind for c in diff_runs(base, new)] == ["sama", "sama"]


def test_diff_marks_a_change_the_ai_review_made():
    base = [_r("Toko A naik.", "Inconclusive")]
    new = [_r("Toko A naik.", "Entailed", reasoning="x | Ditinjau AI: sel ...")]
    [change] = diff_runs(base, new)
    assert change.ai_review


def test_diff_pairs_an_anchor_only_quote_with_the_sentence_that_contains_it():
    """When the model's anchor is not found, the quote IS the anchor ('sebesar 109,2'); in the
    next run it may be located and become the whole sentence. Same claim."""
    base = [_r("sebesar 109,2", "Entailed", "value", 109.2, "Indeks > Usia 31-40")]
    new = [_r("Indeks tertinggi pada usia 20-30 dan 31-40, masing-masing 116,1 dan 109,2.",
              "Entailed", "value", 109.2, "Indeks > Usia 31-40")]
    [change] = diff_runs(base, new)
    assert change.kind == "sama"


def test_a_claim_restructured_into_other_facts_of_the_same_sentence_is_not_flagged():
    """Five per-group facts became one 'seluruh kelompok' fact for the same sentence, with the
    same verdict. That is a different shape of the same check, not a lost Sesuai."""
    q = "Indeks berada pada level optimis pada seluruh kelompok pengeluaran."
    base = [_r(q, "Entailed", "above_threshold", 100.0, f"Indeks > Kelompok {i}") for i in range(3)]
    new = [_r(q, "Entailed", "above_threshold", 100.0, "Indeks (seluruh kelompok)")]
    changes = diff_runs(base, new)
    assert {c.kind for c in changes} <= {"sama", "dibentuk_ulang"}
    assert not any(c.needs_review for c in changes)


def test_a_restructured_sentence_with_a_different_verdict_is_still_flagged():
    q = "Indeks berada pada level optimis pada seluruh kelompok pengeluaran."
    base = [_r(q, "Entailed", "above_threshold", 100.0, "Indeks > Kelompok A")]
    new = [_r(q, "Refuted", "above_threshold", 100.0, "Indeks (seluruh kelompok)")]
    assert any(c.needs_review for c in diff_runs(base, new))
